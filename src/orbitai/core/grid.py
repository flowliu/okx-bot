# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""中性网格核心策略

设计:
  - 上方 N/2 个格位:做空循环。挂 sell 开空 → 成交后在下一格挂 buy 平空 → 成交后回 open
  - 下方 N/2 个格位:做多循环。挂 buy 开多 → 成交后在上一格挂 sell 平多 → 成交后回 open
  - 每个格位独立维护状态,互不影响

风控:
  - 主循环每 tick 检查最新价是否跌破 [center*(1-RANGE-STOP), center*(1+RANGE+STOP)]
    跳出 → 市价清仓 + 撤所有单 + 退出

注意:
  - 必须在双向持仓模式下运行(脚本启动时会自动切)
  - 杠杆在启动时设置为 LEVERAGE
"""
import math
import time
import signal
import sys
from decimal import Decimal, ROUND_DOWN
from loguru import logger

from orbitai.data.client import account_api, trade_api, market_api, public_api
from orbitai.config import defaults as config
from orbitai.config import loader as config_loader
from orbitai.data import db as db
from orbitai.util import notify as notify
from orbitai.core import advisor as ai_advisor

# ===== 全局只读: 合约规格 =====
_TICK_SZ: Decimal = Decimal("0.01")   # 价格最小变动单位
_LOT_SZ:  Decimal = Decimal("0.01")   # 张数最小变动单位
_MIN_SZ:  Decimal = Decimal("0.01")   # 最小张数
_CT_VAL:  Decimal = Decimal("0.1")    # 每张面值(ETH)

_running = True
_margin_check_tick = 0   # 每 N 轮检查一次保证金率,避免每轮都查


def _round_price(px: float) -> str:
    return str(Decimal(str(px)).quantize(_TICK_SZ, rounding=ROUND_DOWN))


def _round_sz(sz: float) -> str:
    return str(Decimal(str(sz)).quantize(_LOT_SZ, rounding=ROUND_DOWN))


def _okx_call_with_retry(fn, retries: int = 8, tag: str = "") -> dict:
    """启动期对 OKX REST 调用做指数退避重试，吃 TLS 超时 / 网络异常。

    fn: 无参 callable，返回 OKX 响应 dict。
    全部失败也不抛异常，返回最后一次的响应（含 code+msg），由调用方决定是否继续。
    """
    last = None
    for i in range(retries):
        try:
            r = fn()
            if r.get("code") in _TRANSIENT_OKX_CODES:
                last = r
                wait = min(2 ** i * 0.5, 8.0)
                logger.warning(f"[OKX] {tag} 临时错误 code={r.get('code')} {r.get('msg','')[:60]} attempt {i+1}/{retries}，{wait:.1f}s 后重试")
                time.sleep(wait)
                continue
            return r
        except Exception as e:
            last = {"code": "network", "msg": str(e), "data": []}
            wait = min(2 ** i * 0.5, 8.0)
            logger.warning(f"[OKX] {tag} 网络异常 attempt {i+1}/{retries}: {type(e).__name__}: {str(e)[:100]}，{wait:.1f}s 后重试")
            time.sleep(wait)
    logger.error(f"[OKX] {tag} 重试 {retries} 次均失败，返回最后响应交由调用方处理")
    return last or {"code": "exhausted", "msg": "no response", "data": []}


def _load_instrument():
    """启动时拉一次合约规格,装入全局。这个必须成功才能下单。"""
    global _TICK_SZ, _LOT_SZ, _MIN_SZ, _CT_VAL
    r = _okx_call_with_retry(
        lambda: public_api().get_instruments(instType="SWAP", instId=config.INST_ID),
        tag="get_instruments",
        retries=12,  # 强制成功,加大重试次数
    )
    if r.get("code") != "0" or not r.get("data"):
        raise RuntimeError(f"取合约规格失败(必须成功才能下单): {r}")
    s = r["data"][0]
    _TICK_SZ = Decimal(s["tickSz"])
    _LOT_SZ = Decimal(s["lotSz"])
    _MIN_SZ = Decimal(s["minSz"])
    _CT_VAL = Decimal(s["ctVal"])
    logger.info(f"合约规格: tickSz={_TICK_SZ} lotSz={_LOT_SZ} minSz={_MIN_SZ} ctVal={_CT_VAL}")


def _ensure_position_mode():
    """切换账户到双向持仓模式(中性网格必需)。
    幂等：失败也不阻塞启动 —— 上次设置仍生效。
    """
    r = _okx_call_with_retry(
        lambda: account_api().set_position_mode(posMode="long_short_mode"),
        tag="set_position_mode",
    )
    if r.get("code") == "0":
        logger.info("持仓模式: long_short_mode(双向)")
    elif r.get("code") == "59000":
        logger.info("持仓模式已经是双向,跳过设置")
    else:
        logger.warning(
            f"设置持仓模式失败，跳过（依赖账户上次保存的设置）: {r}"
        )


def _ensure_leverage():
    """设置 ETH-USDT-SWAP 的杠杆。
    幂等：失败也不阻塞启动 —— 上次设置仍生效。
    """
    r = _okx_call_with_retry(
        lambda: account_api().set_leverage(
            instId=config.INST_ID,
            lever=str(config.LEVERAGE),
            mgnMode=config.TD_MODE,
        ),
        tag="set_leverage",
    )
    if r.get("code") == "0":
        logger.info(f"杠杆设置为 {config.LEVERAGE}x ({config.TD_MODE})")
    else:
        logger.warning(f"设置杠杆失败，跳过（保持账户上次设置）: {r}")


def _get_last_price() -> float:
    r = market_api().get_ticker(instId=config.INST_ID)
    if r.get("code") != "0":
        raise RuntimeError(f"取最新价失败: {r}")
    return float(r["data"][0]["last"])


def _build_grid_prices(center: float) -> list[tuple[int, float]]:
    """生成网格价位列表 [(level, price), ...]

    level > 0: 上方做空格位
    level < 0: 下方做多格位
    """
    half = config.GRID_COUNT // 2
    upper = center * (1 + config.RANGE_PCT)
    lower = center * (1 - config.RANGE_PCT)

    levels: list[tuple[int, float]] = []
    if config.GRID_TYPE == "geometric":
        # 上方:在 (center, upper] 取 half 个对数等距点
        ratio_up = (upper / center) ** (1 / half)
        for i in range(1, half + 1):
            levels.append((i, center * (ratio_up ** i)))
        # 下方:在 [lower, center) 取 half 个对数等距点
        ratio_dn = (center / lower) ** (1 / half)
        for i in range(1, half + 1):
            levels.append((-i, center / (ratio_dn ** i)))
    else:
        step_up = (upper - center) / half
        step_dn = (center - lower) / half
        for i in range(1, half + 1):
            levels.append((i, center + step_up * i))
            levels.append((-i, center - step_dn * i))

    return sorted(levels, key=lambda x: x[0])


def _calc_sz_per_grid(center: float) -> float:
    """机械网格：单格张数 = 总名义 / GRID_COUNT。"""
    notional_per_grid = config.TOTAL_USDT * config.LEVERAGE / config.GRID_COUNT
    eth_per_grid = notional_per_grid / center
    sz = eth_per_grid / float(_CT_VAL)
    if sz < float(_MIN_SZ):
        raise ValueError(
            f"单格张数 {sz:.4f} 低于最小张数 {_MIN_SZ}。"
            f"请增加 TOTAL_USDT 或减少 GRID_COUNT。"
        )
    return float(_round_sz(sz))


def _calc_sz_per_ai_order(center: float) -> float:
    """AI 模式：每对 open + close 两笔单 → 分母用 max_orders × 2，乘 0.6 缓冲。

    资金小到算出的 sz < minSz 时 fallback 到 minSz 并打 warning，
    让低资金账户也能跑（单笔利润可能很薄，预期会有提示）。
    """
    max_orders = max(1, int(config_loader.get("AI_MAX_ORDERS_PER_CALL") or 20))
    leverage = max(1, int(config_loader.get("LEVERAGE") or 1))
    total_usdt = float(config_loader.get("TOTAL_USDT") or 0)
    BUFFER = 0.6
    notional_per_order = total_usdt * leverage / (max_orders * 2) * BUFFER
    eth_per_order = notional_per_order / center
    sz = eth_per_order / float(_CT_VAL)
    min_sz = float(_MIN_SZ)
    if sz < min_sz:
        min_notional = min_sz * float(_CT_VAL) * center
        logger.warning(
            f"⚠ 资金不足以铺满 AI 梯度: 算出 sz={sz:.4f} < minSz={min_sz} "
            f"(资金={total_usdt}U 杠杆={leverage}x max_orders={max_orders}) "
            f"自动 fallback 到 minSz；单订单名义约 {min_notional:.1f} USDT，"
            f"利润可能不足覆盖手续费。建议加资金 / 减小 AI_MAX_ORDERS_PER_CALL / 加杠杆"
        )
        return min_sz
    return float(_round_sz(sz))


# OKX 服务端临时性错误：建议重试
_TRANSIENT_OKX_CODES = {
    "50013",  # Systems are busy. Please try again later.
    "50011",  # API rate limit exceeded
    "50001",  # OKX system error
    "50026",  # System maintenance, please try again later
    "50000",  # Body for POST request not properly formatted (偶发网络问题)
    "51149",  # Order placement function is blocked by the platform (短暂)
}


def _safe_place_order(retries: int = 5, **kwargs) -> dict:
    """对 trade_api.place_order 做指数退避重试，仅对 OKX 临时错误重试。

    成功返回响应 dict；重试耗尽抛 RuntimeError。
    """
    last = None
    for i in range(retries):
        try:
            r = trade_api().place_order(**kwargs)
        except Exception as e:
            # 网络异常（含 TLS / 超时）也按临时错误对待
            last = {"code": "network", "msg": str(e), "data": []}
            wait = min(2 ** i * 0.3, 4.0)
            logger.warning(f"[OKX] place_order 网络异常 attempt {i+1}/{retries}: {e}，{wait:.1f}s 后重试")
            time.sleep(wait)
            continue
        code = r.get("code")
        if code == "0":
            return r
        last = r
        if code not in _TRANSIENT_OKX_CODES:
            # 非临时错误（参数错、价格非法等）直接返回让调用方处理
            return r
        # 临时错误：退避后重试
        wait = min(2 ** i * 0.3 + 0.2, 4.0)  # 0.5/0.8/1.4/2.6/4.0
        logger.warning(f"[OKX] place_order code={code} {r.get('msg','')[:60]} attempt {i+1}/{retries}，{wait:.1f}s 后重试")
        time.sleep(wait)
    # 全部失败
    return last or {"code": "unknown", "msg": "retries exhausted", "data": []}


def _place_open(level: int, price: float, direction: str, sz: float) -> str | None:
    """挂开仓单。direction='long' (下方)→ buy 开多; direction='short' (上方)→ sell 开空。
    AI 顾问否决该方向开仓时返回 None，调用方应当把格位保留为空挂（phase=open / ord_id=None），
    由主循环每轮重试一次。"""
    if not ai_advisor.is_open_allowed(direction):
        logger.info(f"[AI否决] 暂停 {direction} 开仓 level={level:+d} px={price:.2f}（AI 趋势过滤）")
        return None
    side = "buy" if direction == "long" else "sell"
    pos_side = direction
    r = trade_api().place_order(
        instId=config.INST_ID,
        tdMode=config.TD_MODE,
        side=side,
        posSide=pos_side,
        ordType="limit",
        px=_round_price(price),
        sz=_round_sz(sz),
    )
    if r.get("code") != "0":
        raise RuntimeError(f"开仓挂单失败 level={level}: {r}")
    return r["data"][0]["ordId"]


def _place_close(level: int, price: float, direction: str, sz: float) -> str:
    """挂平仓单。direction='long' → sell 平多; direction='short' → buy 平空。"""
    side = "sell" if direction == "long" else "buy"
    pos_side = direction
    r = trade_api().place_order(
        instId=config.INST_ID,
        tdMode=config.TD_MODE,
        side=side,
        posSide=pos_side,
        ordType="limit",
        px=_round_price(price),
        sz=_round_sz(sz),
    )
    if r.get("code") != "0":
        raise RuntimeError(f"平仓挂单失败 level={level}: {r}")
    return r["data"][0]["ordId"]


def _grid_ratio() -> float:
    """每一格的等比因子。多头格成交后,close 价 = open 价 * ratio;空头反之。"""
    half = config.GRID_COUNT // 2
    return (1 + config.RANGE_PCT) ** (1 / half)


def _adjacent_price(level: int, direction: str) -> float:
    """获取平仓挂单价。

    设计:
      - long 格 (level<0) 平仓挂在更高价: 沿 +1 方向找第一个存在的格位
      - short 格 (level>0) 平仓挂在更低价: 沿 -1 方向找
      - 跨过 level=0 的中心(不存在该格)
      - 找不到(已到区间外缘): 用等比因子外推一格

    注意:用 _build_grid_prices(center) 重算原始网格价,
    不查 db.price(因为格位进入 close 阶段时 price 字段会被改成 close 价)。
    """
    center = float(db.get_meta("center_price"))
    original = dict(_build_grid_prices(center))
    step = +1 if direction == "long" else -1
    half = config.GRID_COUNT // 2

    target = level + step
    while target == 0 or (target not in original and -half <= target <= half):
        target += step

    if target in original:
        return original[target]

    # 区间外缘:用等比外推一格
    ratio = _grid_ratio()
    return original[level] * (ratio if direction == "long" else 1 / ratio)


def initialize(center: float):
    """初始化网格:计算价位、计算单格张数、对每格挂开仓单、写入 DB。"""
    db.init_db()

    # 检查 DB 是否已有状态
    existing = db.all_grids()
    if existing:
        logger.warning(
            f"DB 中已有 {len(existing)} 个格位状态。"
            f"如需重新部署请先运行 reset.py(尚未实现,可手动 rm grid.db)"
        )
        return

    sz = _calc_sz_per_grid(center)
    logger.info(f"中心价={center}  单格张数={sz}  单格名义价值≈{sz * float(_CT_VAL) * center:.2f} USDT")

    db.set_meta("center_price", str(center))
    db.set_meta("started_at", str(int(time.time())))

    prices = _build_grid_prices(center)
    logger.info(f"网格价位({len(prices)} 格):")
    for level, px in prices:
        logger.info(f"  level={level:+3d}  px={px:.2f}")

    # 挂开仓单
    placed = 0
    vacant = 0
    for level, px in prices:
        direction = "long" if level < 0 else "short"
        try:
            ord_id = _place_open(level, px, direction, sz)
        except Exception as e:
            logger.error(f"挂单失败 level={level} px={px}: {e}")
            db.upsert_grid(level, float(_round_price(px)), direction, "open", sz, None)
            vacant += 1
            continue
        # AI 否决时 ord_id=None,先把格位记下来,主循环会重试
        db.upsert_grid(level, float(_round_price(px)), direction, "open", sz, ord_id)
        if ord_id is None:
            vacant += 1
        else:
            placed += 1
        time.sleep(0.05)  # 简易限频,避免触发 OKX 速率限制

    logger.success(f"初始网格部署完成: 成功挂单 {placed}/{len(prices)}（AI 否决/失败留空 {vacant} 格,主循环重试）")


def _fetch_order_state(ord_id: str, retries: int = 2) -> dict | None:
    """单笔查询订单状态,内置重试。失败返回 None,绝不抛异常。"""
    for attempt in range(retries + 1):
        try:
            r = trade_api().get_order(instId=config.INST_ID, ordId=ord_id)
            if r.get("code") == "0" and r.get("data"):
                return r["data"][0]
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning(f"_fetch_order_state ordId={ord_id} 重试 {retries} 次仍失败: {e}")
            return None
    return None


def _fetch_pending_orders_map(retries: int = 2) -> dict | None:
    """批量拉所有 pending 订单,返回 {ordId: order_dict}。
    优先用 WebSocket cache（毫秒级+省 REST 配额），fallback 到 REST。
    返回 None 表示拉取失败,调用方跳过本轮。
    """
    # WS 优先
    try:
        from orbitai.data import ws_client
        cache = ws_client.get_pending()
        if cache is not None:
            return cache
    except Exception as e:
        logger.warning(f"[WS] cache 读取异常,fallback REST: {e}")
    # REST fallback
    for attempt in range(retries + 1):
        try:
            r = trade_api().get_order_list(instType="SWAP", instId=config.INST_ID)
            if r.get("code") == "0":
                return {o["ordId"]: o for o in r.get("data", [])}
            logger.warning(f"get_order_list 返回非 0: {r}")
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning(f"_fetch_pending_orders_map 重试 {retries} 次仍失败: {e}")
            return None
    return None


def _handle_filled(grid_row):
    """一个格位的当前挂单成交后,推进状态机。"""
    level = grid_row["level"]
    direction = grid_row["direction"]
    phase = grid_row["phase"]
    sz = grid_row["sz"]
    price = grid_row["price"]

    db.log_trade(level, phase,
                 side=("buy" if (phase == "open" and direction == "long") or
                                (phase == "close" and direction == "short") else "sell"),
                 pos_side=direction, price=price, sz=sz,
                 ord_id=grid_row["ord_id"])

    if phase == "open":
        # 开仓成交 → 在相邻格挂平仓单
        target_px = _adjacent_price(level, direction)
        try:
            ord_id = _place_close(level, target_px, direction, sz)
        except Exception as e:
            logger.error(f"挂平仓单失败 level={level}: {e}")
            db.update_grid_order(level, phase="open", ord_id=None)
            return
        db.update_grid_order(level, phase="close", ord_id=ord_id, price=float(_round_price(target_px)))
        logger.success(f"[level={level:+3d}] 开仓成交 → 挂平仓单 @{target_px:.2f}")
    else:
        # 平仓成交 → 一轮完成,回到开仓状态,在原格价再挂开仓单
        center = float(db.get_meta("center_price"))
        original_prices = dict(_build_grid_prices(center))
        original_open_px = original_prices[level]

        try:
            ord_id = _place_open(level, original_open_px, direction, sz)
        except Exception as e:
            logger.error(f"挂开仓单失败 level={level}: {e}")
            db.update_grid_order(level, phase="open", ord_id=None,
                                 price=float(_round_price(original_open_px)))
            return
        # ord_id 可能是 None(AI 否决),也要把 price 改回 open 价,留主循环重试
        db.update_grid_order(level, phase="open", ord_id=ord_id,
                             price=float(_round_price(original_open_px)))
        if ord_id is None:
            logger.info(f"[level={level:+3d}] 平仓成交,但 AI 暂停 {direction} 开仓,留空待重试")
        else:
            logger.success(f"[level={level:+3d}] 平仓成交(一轮完成)→ 重挂开仓单 @{original_open_px:.2f}")


def _check_stop_loss(last_px: float) -> bool:
    center = float(db.get_meta("center_price"))
    upper_breach = center * (1 + config.RANGE_PCT) * (1 + config.STOP_LOSS_PCT)
    lower_breach = center * (1 - config.RANGE_PCT) * (1 - config.STOP_LOSS_PCT)
    if last_px > upper_breach:
        msg = f"价格 {last_px} 突破上止损 {upper_breach:.2f},触发清仓"
        logger.error(f"⚠️ {msg}")
        notify.alert(msg, dedup_key="stop_loss_upper")
        return True
    if last_px < lower_breach:
        msg = f"价格 {last_px} 跌破下止损 {lower_breach:.2f},触发清仓"
        logger.error(f"⚠️ {msg}")
        notify.alert(msg, dedup_key="stop_loss_lower")
        return True
    return False


# 风控暂停截止时间（unix 秒）。任何写挂单流程进来都先检查 _is_paused()
_paused_until_ts: float = 0.0
_last_daily_loss_check: float = 0.0


def _is_paused() -> bool:
    return _paused_until_ts > time.time()


def _pause_for(hours: float, reason: str) -> None:
    global _paused_until_ts
    until = time.time() + hours * 3600
    if until > _paused_until_ts:
        _paused_until_ts = until
        logger.error(f"⚠️ 风控暂停: {reason}（{hours}h，恢复时刻 {time.strftime('%H:%M', time.localtime(until))}）")


def _check_margin_ratio() -> bool:
    """保证金率检查。低于阈值时：撤所有 AI open 单 + 暂停 N 小时挂新单。
    返回 True 表示安全，False 表示已触发风控。
    """
    try:
        r = account_api().get_account_balance()
    except Exception as e:
        logger.warning(f"查询保证金率失败(忽略本轮): {e}")
        return True
    if r.get("code") != "0" or not r.get("data"):
        return True
    d = r["data"][0]
    mgn_ratio_str = d.get("mgnRatio", "")
    if not mgn_ratio_str:
        return True
    try:
        mgn = float(mgn_ratio_str)
    except ValueError:
        return True
    if mgn <= 0:
        return True
    threshold = float(config_loader.get("MIN_MARGIN_RATIO") or 0.30)
    if mgn < threshold:
        msg = f"保证金率 {mgn:.3f} < 阈值 {threshold}：撤所有 open 单 + 暂停挂新单"
        notify.alert(f"⚠️ {msg}", dedup_key="low_margin_action")
        pause_h = float(config_loader.get("MARGIN_LOW_PAUSE_HOURS") or 1)
        _pause_for(pause_h, f"保证金率低 {mgn:.3f}<{threshold}")
        try:
            cancel_all_orders_ai()
        except Exception as e:
            logger.warning(f"低保证金触发撤单失败: {e}")
        return False
    return True


def _check_daily_loss() -> bool:
    """日亏熔断：每 5 分钟拉一次今日成交账单，累计净亏超阈值则撤所有 + 暂停 N 小时。
    返回 True 表示安全。
    """
    global _last_daily_loss_check
    max_loss = float(config_loader.get("MAX_DAILY_LOSS_USDT") or 0)
    if max_loss <= 0:
        return True  # 没配阈值即关闭日亏熔断
    now = time.time()
    if now - _last_daily_loss_check < 300:
        return not _is_paused()
    _last_daily_loss_check = now
    try:
        from orbitai.cli import stats as stats_mod
        from datetime import datetime
        today = datetime.now().date()
        start = datetime.combine(today, datetime.min.time())
        bills = stats_mod.fetch_bills_in_range(
            int(start.timestamp() * 1000),
            int(now * 1000),
        )
        by_day = stats_mod.summarize_by_day(bills)
        today_str = today.strftime("%Y-%m-%d")
        net = float(by_day.get(today_str, {}).get("net_pnl", 0.0))
    except Exception as e:
        logger.warning(f"日亏熔断检查失败(忽略本轮): {e}")
        return True
    if -net >= max_loss:
        pause_h = float(config_loader.get("DAILY_LOSS_PAUSE_HOURS") or 4)
        notify.alert(
            f"🛑 日亏熔断: 今日净 {net:+.2f} USDT 触达 -{max_loss}，撤所有 open 单 + 暂停 {pause_h}h",
            dedup_key=f"daily_loss_{today_str}",
        )
        _pause_for(pause_h, f"当日净亏 {net:.2f}")
        try:
            cancel_all_orders_ai()
        except Exception as e:
            logger.warning(f"日亏熔断撤单失败: {e}")
        return False
    return True


def reconcile_on_startup():
    """启动时与 OKX 对账:
      - db 中标记有 ord_id 但 OKX 上不存在 → 查这笔订单状态,推进或清空
      - OKX 上有 pending 单但 db 没记录 → 警告(可能是孤儿单,提示用 reset.py)
    """
    db_rows = db.grids_with_orders()
    if not db_rows:
        return
    pending_map = _fetch_pending_orders_map()
    if pending_map is None:
        logger.warning("启动对账:拉 OKX pending 单失败,跳过对账")
        return

    db_ord_ids = {r["ord_id"] for r in db_rows}
    okx_ord_ids = set(pending_map.keys())

    missing = db_ord_ids - okx_ord_ids
    orphan  = okx_ord_ids - db_ord_ids

    if missing:
        logger.warning(f"启动对账:db 中有 {len(missing)} 笔挂单在 OKX 上消失,逐笔确认状态")
        for r in db_rows:
            if r["ord_id"] not in missing:
                continue
            state = _fetch_order_state(r["ord_id"])
            if not state:
                db.update_grid_order(r["level"], phase=r["phase"], ord_id=None)
                continue
            s = state.get("state")
            if s == "filled":
                logger.info(f"对账推进:[level={r['level']:+3d}] {r['phase']} 单期间已成交")
                try:
                    _handle_filled(r)
                except Exception as e:
                    logger.exception(f"对账推进失败 level={r['level']}: {e}")
            else:
                db.update_grid_order(r["level"], phase=r["phase"], ord_id=None)

    if orphan:
        msg = f"启动对账:OKX 上有 {len(orphan)} 笔挂单未记录在 db,可能是上次异常残留。建议 Ctrl+C 后跑 reset.py 清理"
        logger.warning(msg)
        notify.warn(msg, dedup_key="orphan_orders")


def cancel_all_orders():
    """撤销所有当前挂着的网格订单。"""
    rows = db.grids_with_orders()
    if not rows:
        return
    logger.info(f"撤销 {len(rows)} 笔挂单 ...")
    for r in rows:
        try:
            trade_api().cancel_order(instId=config.INST_ID, ordId=r["ord_id"])
        except Exception as e:
            logger.warning(f"撤单异常 level={r['level']} ordId={r['ord_id']}: {e}")
        db.update_grid_order(r["level"], phase=r["phase"], ord_id=None)
        time.sleep(0.05)
    logger.info("挂单已全部撤销")


def close_all_positions():
    """市价平掉所有持仓(止损触发时用)。"""
    r = account_api().get_positions(instType="SWAP", instId=config.INST_ID)
    if r.get("code") != "0":
        logger.error(f"查持仓失败: {r}")
        return
    for pos in r["data"]:
        sz = float(pos.get("pos", 0))
        if abs(sz) < 1e-9:
            continue
        pos_side = pos["posSide"]  # long / short
        side = "sell" if pos_side == "long" else "buy"
        close_sz = abs(sz)
        rr = trade_api().place_order(
            instId=config.INST_ID,
            tdMode=config.TD_MODE,
            side=side,
            posSide=pos_side,
            ordType="market",
            sz=_round_sz(close_sz),
        )
        if rr.get("code") == "0":
            logger.success(f"已市价平仓 posSide={pos_side} sz={close_sz}")
        else:
            logger.error(f"市价平仓失败 posSide={pos_side}: {rr}")


def _signal_handler(signum, frame):
    global _running
    logger.warning(f"收到信号 {signum},准备优雅退出 ...")
    _running = False


def _retry_vacant_open_grids():
    """扫一遍 phase=open / ord_id=None 的格位,如果 AI 现在放行了就把单挂回去。
    AI 否决的方向继续留空。"""
    rows = db.vacant_open_grids()
    if not rows:
        return
    for r in rows:
        direction = r["direction"]
        try:
            ord_id = _place_open(r["level"], r["price"], direction, r["sz"])
        except Exception as e:
            logger.warning(f"[重挂] level={r['level']:+d} 仍失败: {e}")
            continue
        if ord_id is None:
            # AI 还在否决,保持空挂
            continue
        db.update_grid_order(r["level"], phase="open", ord_id=ord_id, price=r["price"])
        logger.success(f"[重挂] level={r['level']:+3d} {direction} @{r['price']:.2f} → ord_id={ord_id}")
        time.sleep(0.05)


def _ai_maybe_recenter(last_px: float) -> bool:
    """根据 AI 建议尝试重居中。返回 True 表示已重发网格。
    需要 config.AI_AUTO_RECENTER=True 才会真正执行,否则只打日志建议。"""
    advice = ai_advisor.current_advice()
    if advice.recenter_to is None:
        return False
    old_center = float(db.get_meta("center_price") or last_px)
    new_center = advice.recenter_to
    drift = abs(new_center - old_center) / old_center if old_center else 0
    if drift < config.AI_RECENTER_MIN_DRIFT_PCT:
        return False  # 噪声,跳过
    if not config.AI_AUTO_RECENTER:
        logger.info(f"[AI建议重居中] {old_center:.2f} → {new_center:.2f} "
                    f"(漂移 {drift*100:.2f}%),但 AI_AUTO_RECENTER=False,仅记录")
        return False

    logger.warning(f"[AI重居中] {old_center:.2f} → {new_center:.2f} (漂移 {drift*100:.2f}%) | "
                   f"{advice.reason[:80]}")
    notify.warn(f"AI 触发重居中: {old_center:.2f} → {new_center:.2f}", dedup_key="recenter")

    # 撤销 phase=open 的挂单,保留 phase=close 的持仓出场单
    open_orders = [r for r in db.grids_with_orders() if r["phase"] == "open"]
    for r in open_orders:
        try:
            trade_api().cancel_order(instId=config.INST_ID, ordId=r["ord_id"])
        except Exception as e:
            logger.warning(f"重居中撤单异常 level={r['level']}: {e}")
        db.update_grid_order(r["level"], phase="open", ord_id=None)
        time.sleep(0.05)

    # 重算价位,删掉旧的 open 格,挂新格
    sz = _calc_sz_per_grid(new_center)
    db.set_meta("center_price", str(new_center))
    new_prices = _build_grid_prices(new_center)
    # 仅替换 open 格:close 格保持原状(它们对应已有持仓,等老价位平仓)
    existing = {r["level"]: r for r in db.all_grids()}
    placed = 0
    for level, px in new_prices:
        direction = "long" if level < 0 else "short"
        prev = existing.get(level)
        if prev and prev["phase"] == "close":
            continue  # 已有未平仓位,跳过(它的 close 单仍挂在旧价)
        try:
            ord_id = _place_open(level, px, direction, sz)
        except Exception as e:
            logger.error(f"重居中挂单失败 level={level}: {e}")
            db.upsert_grid(level, float(_round_price(px)), direction, "open", sz, None)
            continue
        db.upsert_grid(level, float(_round_price(px)), direction, "open", sz, ord_id)
        if ord_id is not None:
            placed += 1
        time.sleep(0.05)
    logger.success(f"[AI重居中] 完成,新中心 {new_center:.2f},重挂 {placed} 格")
    return True


# ============================================================
# AI 完全驱动模式
# ============================================================
MIN_PROFIT_PCT = 0.0016  # 单对最小利润空间 0.16%(覆盖手续费),低于此值自动撑到此值;比 validator 阈值略高,留浮点 buffer


def _normalize_ai_order(o, last_px: float):
    """LLM 偶尔会给低于最小利润的 close 价,自动撑到 MIN_PROFIT_PCT。
    - limit 单:以 open_price 为基准
    - market 单:以 last_px 为基准(实际成交价)
    """
    is_market = getattr(o, "order_type", "limit") == "market"
    base = last_px if is_market else o.open_price
    if base <= 0:
        return o
    if o.side == "long":
        gap = (o.close_price - base) / base
        if gap < MIN_PROFIT_PCT:
            o.close_price = round(base * (1 + MIN_PROFIT_PCT), 2)
    elif o.side == "short":
        gap = (base - o.close_price) / base
        if gap < MIN_PROFIT_PCT:
            o.close_price = round(base * (1 - MIN_PROFIT_PCT), 2)
    return o


def _validate_ai_order(o, last_px: float) -> tuple[bool, str]:
    """检查 AI 给的一对价格是否合理。返回 (ok, reason)。

    limit 单:
      long  → open_price < last_px (限价买在下方) 且 close_price > open_price
      short → open_price > last_px (限价卖在上方) 且 close_price < open_price

    market 单(立即成交):
      跳过 open_price 与 last_px 的位置校验(实际按市价成交)
      close_price 与 last_px 的差距要满足最小利润(因 market 大概率在 last_px 附近成交)
    """
    is_market = getattr(o, "order_type", "limit") == "market"

    if o.side == "long":
        if is_market:
            if o.close_price <= last_px:
                return False, f"market long 的 close {o.close_price} 必须高于 last_px {last_px}"
            if (o.close_price - last_px) / last_px < 0.0015:
                return False, f"market long close 距 last_px < 0.15%,无法覆盖 taker 手续费"
        else:
            if not (o.open_price < last_px):
                return False, f"long open_price {o.open_price} 不低于 last_px {last_px}"
            if o.close_price <= o.open_price:
                return False, f"long close {o.close_price} 必须大于 open {o.open_price}"
            if (last_px - o.open_price) / last_px < 0.0008:
                return False, f"long open_price {o.open_price} 离 {last_px} 太近,易立即成交"
            if (o.close_price - o.open_price) / o.open_price < 0.0015:
                return False, f"long close-open 差 < 0.15%,无法覆盖手续费"
    elif o.side == "short":
        if is_market:
            if o.close_price >= last_px:
                return False, f"market short 的 close {o.close_price} 必须低于 last_px {last_px}"
            if (last_px - o.close_price) / last_px < 0.0015:
                return False, f"market short close 距 last_px < 0.15%,无法覆盖 taker 手续费"
        else:
            if not (o.open_price > last_px):
                return False, f"short open_price {o.open_price} 不高于 last_px {last_px}"
            if o.close_price >= o.open_price:
                return False, f"short close {o.close_price} 必须小于 open {o.open_price}"
            if (o.open_price - last_px) / last_px < 0.0008:
                return False, f"short open_price {o.open_price} 离 {last_px} 太近"
            if (o.open_price - o.close_price) / o.open_price < 0.0015:
                return False, f"short open-close 差 < 0.15%,无法覆盖手续费"
    else:
        return False, f"未知 side={o.side}"
    return True, ""


def _ai_cancel_open_phase_slots():
    """撤销所有 phase='open' 的 slot 的挂单（aggressive 重发用）。"""
    rows = db.ai_slots_by_phase("open")
    if not rows:
        return
    for r in rows:
        if r["open_ord_id"]:
            try:
                trade_api().cancel_order(instId=config.INST_ID, ordId=r["open_ord_id"])
            except Exception as e:
                logger.warning(f"撤 slot#{r['id']} 异常: {e}")
        db.ai_slot_update(r["id"], phase="cancelled", open_ord_id=None)
        time.sleep(0.05)


def _ai_place_orders_from_advice(advice, last_px: float, sz: float):
    """读取 advice.orders,逐对挂 open 单,写入 ai_slots。"""
    if not advice.orders:
        logger.info(f"[AI seq={advice.seq}] 未给出订单建议(可能趋势不明朗),本轮空挂")
        return
    placed = 0
    rejected = 0
    normalized = 0
    market_n = 0
    okx_failed = 0
    for o in advice.orders:
        # 在校验前先做利润空间 normalize:LLM 可能给低于阈值的 close 价
        original_close = o.close_price
        _normalize_ai_order(o, last_px)
        if o.close_price != original_close:
            normalized += 1
        ok, why = _validate_ai_order(o, last_px)
        if not ok:
            logger.warning(f"[AI] 拒绝订单 {o}: {why}")
            rejected += 1
            continue
        try:
            if o.order_type == "market":
                ord_id = _place_open_market(o.side, sz)
                market_n += 1
                logger.info(f"[AI] 市价开 {o.side} sz={sz} (close 限价@{o.close_price:.2f})")
            else:
                ord_id = _place_open_raw(o.side, o.open_price, sz)
        except Exception as e:
            err = str(e)
            logger.error(f"[AI] 挂 open 单失败 {o}: {err}")
            okx_failed += 1
            # 保证金不足：本轮没必要继续撞，撞 N 次得到 N 次拒绝
            if "51008" in err or "Insufficient" in err:
                logger.error(
                    f"⚠ 51008 USDT 保证金不足，本轮提前中止；建议检查持仓/挂单/杠杆/单笔 sz "
                    f"(当前 sz={sz})。后续轮继续尝试。"
                )
                break
            continue
        # market 单 open_price 存最近成交价做参考
        open_px_to_store = last_px if o.order_type == "market" else o.open_price
        db.ai_slot_add(o.side, float(_round_price(open_px_to_store)),
                       float(_round_price(o.close_price)), sz, ord_id)
        placed += 1
        time.sleep(0.05)

    summary = (f"[AI seq={advice.seq}] placed={placed} (market={market_n}) "
               f"rejected={rejected} okx_failed={okx_failed} normalized={normalized}")
    total_tried = placed + okx_failed
    if placed == 0 and total_tried > 0:
        # OKX 全拒（如 50013 持续繁忙）
        logger.error(f"⚠ 全部挂单失败（OKX 拒绝 {okx_failed} 单），无委托落地  {summary}")
    elif okx_failed > 0:
        logger.warning(f"部分挂单失败：成功 {placed} / OKX 拒 {okx_failed}  {summary}")
    elif placed > 0:
        logger.success(f"挂单完成  {summary}")
    else:
        # placed=0 且 okx_failed=0 → 全在校验阶段拒了
        logger.info(f"本轮无可挂订单（全部被本地校验拒）  {summary}")


def _place_open_raw(side: str, price: float, sz: float) -> str:
    """绕过 AI veto 的开仓挂单（AI 模式下决策已在 advice 里做了，这里直接挂）。"""
    okx_side = "buy" if side == "long" else "sell"
    r = _safe_place_order(
        instId=config.INST_ID,
        tdMode=config.TD_MODE,
        side=okx_side,
        posSide=side,
        ordType="limit",
        px=_round_price(price),
        sz=_round_sz(sz),
    )
    if r.get("code") != "0":
        raise RuntimeError(f"[AI] 挂 open 失败 side={side} px={price}: {r}")
    return r["data"][0]["ordId"]


def _place_close_raw(side: str, price: float, sz: float) -> str:
    okx_side = "sell" if side == "long" else "buy"
    r = _safe_place_order(
        instId=config.INST_ID,
        tdMode=config.TD_MODE,
        side=okx_side,
        posSide=side,
        ordType="limit",
        px=_round_price(price),
        sz=_round_sz(sz),
    )
    if r.get("code") != "0":
        raise RuntimeError(f"[AI] 挂 close 失败 side={side} px={price}: {r}")
    return r["data"][0]["ordId"]


def _place_open_market(side: str, sz: float) -> str:
    """市价开仓:立即成交,吃 taker 手续费。"""
    okx_side = "buy" if side == "long" else "sell"
    r = _safe_place_order(
        instId=config.INST_ID,
        tdMode=config.TD_MODE,
        side=okx_side,
        posSide=side,
        ordType="market",
        sz=_round_sz(sz),
    )
    if r.get("code") != "0":
        raise RuntimeError(f"[AI] 市价开仓失败 side={side}: {r}")
    return r["data"][0]["ordId"]


def _ai_handle_slot_state(slot, pending_map):
    """检查一个 slot 当前挂着的那笔单的状态,推进 phase。"""
    phase = slot["phase"]
    if phase == "open":
        ord_id = slot["open_ord_id"]
        if not ord_id:
            return
        if ord_id in pending_map:
            return  # 还挂着
        state = _fetch_order_state(ord_id)
        if not state:
            return
        if state.get("state") == "filled":
            logger.info(f"[AI slot#{slot['id']}] {slot['side']} open 成交 → 挂 close @{slot['close_price']:.2f}")
            db.log_trade(level=0, phase="open",
                         side=("buy" if slot["side"] == "long" else "sell"),
                         pos_side=slot["side"], price=slot["open_price"], sz=slot["sz"], ord_id=ord_id)
            try:
                close_id = _place_close_raw(slot["side"], slot["close_price"], slot["sz"])
            except Exception as e:
                logger.error(f"[AI slot#{slot['id']}] 挂 close 失败: {e}")
                db.ai_slot_update(slot["id"], phase="open", open_ord_id=None)
                return
            db.ai_slot_update(slot["id"], phase="close",
                              open_ord_id=ord_id, close_ord_id=close_id)
        elif state.get("state") == "canceled":
            db.ai_slot_update(slot["id"], phase="cancelled", open_ord_id=None)
    elif phase == "close":
        ord_id = slot["close_ord_id"]
        if not ord_id:
            return
        if ord_id in pending_map:
            return
        state = _fetch_order_state(ord_id)
        if not state:
            return
        if state.get("state") == "filled":
            logger.success(f"[AI slot#{slot['id']}] {slot['side']} close 成交 "
                           f"open={slot['open_price']:.2f}→close={slot['close_price']:.2f} ✓")
            db.log_trade(level=0, phase="close",
                         side=("sell" if slot["side"] == "long" else "buy"),
                         pos_side=slot["side"], price=slot["close_price"], sz=slot["sz"], ord_id=ord_id)
            db.ai_slot_update(slot["id"], phase="done")
        elif state.get("state") == "canceled":
            # close 单被人为撤了 → 仓位还在,需要人工介入
            logger.warning(f"[AI slot#{slot['id']}] close 单被取消！仓位悬空,请人工处理")
            db.ai_slot_update(slot["id"], phase="open", close_ord_id=None)


def _ai_auto_cancel(last_px: float) -> tuple[int, int]:
    """轮询期间的自动撤单策略，返回 (drift_cancelled, stale_cancelled)。

    两条规则只针对 phase='open' 的未成交开仓单：
      1) 漂移：|open_price - last_px| / last_px > AI_AUTO_CANCEL_DRIFT_PCT
      2) 陈旧：now - created_at > AI_AUTO_CANCEL_STALE_SEC

    撤掉的 slot 标记为 cancelled，不影响已成交持仓的 close 单。
    下一轮 AI 决策（最多 60s 后）会重新铺梯度，让出来的保证金可被复用。
    """
    if not config_loader.get("AI_AUTO_CANCEL"):
        return 0, 0
    drift_thr = float(config_loader.get("AI_AUTO_CANCEL_DRIFT_PCT") or 0.012)
    stale_thr = int(config_loader.get("AI_AUTO_CANCEL_STALE_SEC") or 300)
    now = int(time.time())
    drift_n = 0
    stale_n = 0
    for s in db.ai_slots_active():
        if s["phase"] != "open" or not s.get("open_ord_id"):
            continue
        drift = abs(s["open_price"] - last_px) / last_px if last_px > 0 else 0
        age = now - int(s.get("created_at") or now)
        reason = None
        if drift > drift_thr:
            reason = f"漂移 {drift * 100:.2f}% > {drift_thr * 100:.2f}%"
        elif age > stale_thr:
            reason = f"陈旧 {age}s > {stale_thr}s"
        if not reason:
            continue
        try:
            r = trade_api().cancel_order(instId=config.INST_ID, ordId=s["open_ord_id"])
            if r.get("code") == "0":
                db.ai_slot_update(s["id"], phase="cancelled")
                logger.info(
                    f"[AI 自动撤] slot#{s['id']} {s['side']} open={s['open_price']:.2f} "
                    f"last_px={last_px:.2f} 理由={reason}"
                )
                if drift > drift_thr:
                    drift_n += 1
                else:
                    stale_n += 1
            else:
                # 51400=订单不存在（已成交/已撤），同步本地状态
                if r.get("code") in ("51400", "51401", "51402"):
                    db.ai_slot_update(s["id"], phase="cancelled")
        except Exception as e:
            logger.warning(f"[AI 自动撤] slot#{s['id']} 撤单异常: {e}")
    return drift_n, stale_n


def ai_main_loop():
    """AI 完全驱动模式的主循环。"""
    global _margin_check_tick
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):  # Windows: webui 用 CTRL_BREAK_EVENT 优雅停
        signal.signal(signal.SIGBREAK, _signal_handler)

    db.init_db()
    logger.info(f"进入 AI 驱动主循环,轮询 {config_loader.get('POLL_INTERVAL')}s,AI 间隔 {config.AI_INTERVAL_SEC}s")
    notify.info(f"AI 驱动模式启动:{config.INST_ID} 资金 {config.TOTAL_USDT}U 杠杆 {config.LEVERAGE}x")
    ai_advisor.start()
    # 启动 WebSocket private 频道（替代 REST 拉 pending 单）。失败时主循环自动 fallback REST
    try:
        from orbitai.data import ws_client
        ws_client.start(config.INST_ID)
    except Exception as e:
        logger.warning(f"[WS] 启动失败,继续 REST 轮询: {e}")

    # 启动时拉一次价初始化单 slot 张数；网络波动会重试,不让 bot 直接挂掉
    sz = None
    while _running and sz is None:
        try:
            sz = _calc_sz_per_ai_order(_get_last_price())
            logger.info(f"AI 单订单张数={sz} "
                        f"(资金={config.TOTAL_USDT}U 杠杆={config.LEVERAGE}x "
                        f"max_orders={config_loader.get('AI_MAX_ORDERS_PER_CALL')})")
        except Exception as e:
            logger.warning(f"启动初始化拉价失败,{config_loader.get('POLL_INTERVAL')}s 后重试: {e}")
            time.sleep(config_loader.get('POLL_INTERVAL'))
    if sz is None:
        logger.info("收到退出信号,启动阶段直接退出")
        ai_advisor.stop()
        return
    last_seen_seq = 0
    try:
        while _running:
            try:
                last_px = _get_last_price()

                # 风控:走出 ±(RANGE+STOP) 后清仓
                center = float(db.get_meta("center_price") or last_px)
                if not db.get_meta("center_price"):
                    db.set_meta("center_price", str(last_px))
                if _check_stop_loss(last_px):
                    cancel_all_orders_ai()
                    close_all_positions()
                    notify.alert("止损流程完成", dedup_key="stop_loss_done")
                    return

                _margin_check_tick = (_margin_check_tick + 1) % 10
                if _margin_check_tick == 0:
                    _check_margin_ratio()

                # 拉 pending 单
                pending_map = _fetch_pending_orders_map()
                if pending_map is None:
                    time.sleep(config_loader.get('POLL_INTERVAL'))
                    continue

                # 推进所有活跃 slot 的状态
                for s in db.ai_slots_active():
                    try:
                        _ai_handle_slot_state(s, pending_map)
                    except Exception as e:
                        logger.exception(f"[AI] 处理 slot#{s['id']} 异常: {e}")

                # 轮询期间自动撤单：漂移 / 陈旧的 open 单
                try:
                    d, s = _ai_auto_cancel(last_px)
                    if d or s:
                        logger.info(f"[AI 自动撤] 本轮撤掉 drift={d} stale={s}")
                except Exception as e:
                    logger.warning(f"[AI 自动撤] 异常: {e}")

                # 日亏熔断检查（内部 5min 节流，便宜）
                _check_daily_loss()

                # 看 AI 是否有新建议:激进模式下,每次 seq 变化都重发
                advice = ai_advisor.current_advice()
                if advice.seq != last_seen_seq and advice.seq > 0:
                    last_seen_seq = advice.seq
                    if _is_paused():
                        remain = int(_paused_until_ts - time.time())
                        logger.info(f"[AI seq={advice.seq}] 风控暂停中 {remain}s，跳过本轮挂单")
                    else:
                        # 撤所有 phase=open 的未成交单,按新 advice 重挂
                        _ai_cancel_open_phase_slots()
                        _ai_place_orders_from_advice(advice, last_px, sz)

            except Exception as e:
                logger.exception(f"AI 主循环异常: {e}")
            time.sleep(config_loader.get('POLL_INTERVAL'))
    finally:
        ai_advisor.stop()

    try:
        from orbitai.data import ws_client
        ws_client.stop()
    except Exception:
        pass

    logger.info("退出流程: 撤所有 AI open 单(持仓 close 单保留待平)")
    cancel_all_orders_ai()
    notify.info("AI 驱动模式已停止(挂单已撤,持仓保留)")


def cancel_all_orders_ai():
    """撤销所有 AI slot 的 open 阶段单(close 单留着等持仓平掉)。"""
    rows = db.ai_slots_by_phase("open")
    for r in rows:
        if r["open_ord_id"]:
            try:
                trade_api().cancel_order(instId=config.INST_ID, ordId=r["open_ord_id"])
            except Exception as e:
                logger.warning(f"撤 slot#{r['id']} open 单异常: {e}")
        db.ai_slot_update(r["id"], phase="cancelled", open_ord_id=None)
        time.sleep(0.05)


def main_loop():
    global _margin_check_tick
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):  # Windows: webui 用 CTRL_BREAK_EVENT 优雅停
        signal.signal(signal.SIGBREAK, _signal_handler)

    logger.info(f"进入主循环,轮询间隔 {config_loader.get('POLL_INTERVAL')}s,Ctrl+C 退出")
    notify.info(f"网格策略已启动:{config.INST_ID} 资金 {config.TOTAL_USDT}U 杠杆 {config.LEVERAGE}x")
    ai_advisor.start()
    _vacant_check_tick = 0
    _recenter_check_tick = 0
    try:
        while _running:
            try:
                last_px = _get_last_price()

                # 风控:跳出区间立即清仓退出
                if _check_stop_loss(last_px):
                    cancel_all_orders()
                    close_all_positions()
                    logger.error("已触发止损,策略停止。请人工介入决定是否重启。")
                    notify.alert("止损流程完成:已撤所有挂单 + 市价平所有持仓 + 策略停止",
                                 dedup_key="stop_loss_done")
                    return

                # 风控:每 10 轮(30 秒)检查一次保证金率
                _margin_check_tick = (_margin_check_tick + 1) % 10
                if _margin_check_tick == 0:
                    _check_margin_ratio()

                # 每 ~10 轮(30 秒)重试一次被 AI 否决的空挂格位
                _vacant_check_tick = (_vacant_check_tick + 1) % 10
                if _vacant_check_tick == 0:
                    _retry_vacant_open_grids()

                # 每 ~20 轮(60 秒)看一次 AI 是否建议重居中
                _recenter_check_tick = (_recenter_check_tick + 1) % 20
                if _recenter_check_tick == 0:
                    _ai_maybe_recenter(last_px)

                # 一次性拉所有 pending 挂单,降低请求数 & 减少代理超时概率
                pending_map = _fetch_pending_orders_map()
                if pending_map is None:
                    logger.warning("本轮拉取 pending 订单失败,跳过这一轮(网络/代理问题)")
                    time.sleep(config_loader.get('POLL_INTERVAL'))
                    continue

                # 对每个 DB 里有挂单的格位:
                #   - 还在 pending → 单还挂着,跳过
                #   - 不在 pending → 已成交或被撤,单查一次确认状态
                for r in db.grids_with_orders():
                    if r["ord_id"] in pending_map:
                        continue  # 还挂着
                    state = _fetch_order_state(r["ord_id"])
                    if not state:
                        continue  # 查询失败,留待下轮
                    s = state.get("state")
                    if s == "filled":
                        logger.info(f"[level={r['level']:+3d}] {r['phase']} 单已成交 ordId={r['ord_id']}")
                        try:
                            _handle_filled(r)
                        except Exception as e:
                            logger.exception(f"处理成交失败 level={r['level']}: {e}")
                    elif s == "canceled":
                        logger.warning(f"[level={r['level']:+3d}] 挂单被取消(可能人工操作),DB 标记清空")
                        db.update_grid_order(r["level"], phase=r["phase"], ord_id=None)
                    # 其他罕见状态 (mmp_canceled 等) 暂当作消失处理,等下轮再核
            except Exception as e:
                logger.exception(f"主循环异常: {e}")

            time.sleep(config_loader.get('POLL_INTERVAL'))
    finally:
        ai_advisor.stop()

    # 优雅退出
    logger.info("退出流程: 撤销所有挂单(持仓保留)")
    cancel_all_orders()
    logger.info("退出完成")
    notify.info("网格策略已停止(人工 Ctrl+C),持仓保留,挂单已撤")
