# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""回测引擎（MVP）：拉历史 K 线 + 重放 AI 决策 + 模拟成交。

简化撮合模型：
  - 每根 K 线按现实顺序 [open, high, low, close] 走价
  - 挂单 (open_price, close_price) 当 K 线的 [low, high] 覆盖到价格时算成交
  - 手续费按 OKX 默认 maker 0.02% × 2 (open+close maker)；market 单按 taker 0.05%
  - 不考虑滑点 / 部分成交

输出：
  - 每对 slot 的成交情况、净利
  - 累计 PnL 曲线
  - 总成交率 / 胜率 / 平均利润

用法：
  orbitai-backtest --bars 1000 --bar 1m --inst ETH-USDT-SWAP
  orbitai-backtest --bars 200  --bar 5m --dry-run  # 不调 AI，固定 ladder 策略
"""
import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from orbitai.config import loader as cfg
from orbitai.core import advisor


@dataclass
class Trade:
    side: str
    open_px: float
    close_px: float
    sz: float
    open_ts: int = 0
    close_ts: int = 0
    fee: float = 0.0
    pnl_gross: float = 0.0
    state: str = "open"  # open / open_filled / done / unfilled

    @property
    def pnl_net(self) -> float:
        return self.pnl_gross - self.fee


def _fetch_klines(inst_id: str, bar: str, limit: int) -> list[tuple[int, float, float, float, float, float]]:
    """拉历史 K 线。OKX get_history_candlesticks 单次 100 根上限，需要分页。
    返回按时间升序的 (ts_ms, o, h, l, c, v)。
    """
    from orbitai.data.client import market_api
    out: list = []
    api = market_api()
    after = ""
    while len(out) < limit:
        batch_n = min(100, limit - len(out))
        try:
            r = api.get_history_candlesticks(
                instId=inst_id, bar=bar, limit=str(batch_n),
                after=after or "",
            )
        except Exception as e:
            logger.warning(f"K 线分页拉取异常: {e}")
            break
        if r.get("code") != "0" or not r.get("data"):
            logger.warning(f"K 线接口非 0: {r}")
            break
        data = r["data"]
        for row in data:
            ts, o, h, l, c, v = int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
            out.append((ts, o, h, l, c, v))
        # 翻页：用最早一根的 ts 作为 after
        after = data[-1][0]
        time.sleep(0.1)
    out.sort(key=lambda x: x[0])
    return out[:limit]


def _build_ladder_dry(last_px: float, max_n: int) -> list[dict]:
    """无 AI 时的兜底固定 ladder（每对 ±0.3% open, ±0.6% close）。"""
    orders = []
    for i in range(max_n // 2):
        d = 0.003 * (1 + i * 0.5)
        orders.append({
            "side": "long",
            "open_price": last_px * (1 - d),
            "close_price": last_px * (1 - d + 0.006),
            "order_type": "limit",
        })
        orders.append({
            "side": "short",
            "open_price": last_px * (1 + d),
            "close_price": last_px * (1 + d - 0.006),
            "order_type": "limit",
        })
    return orders


def _ai_decide(klines_recent: list, last_px: float, bar: str) -> list[dict]:
    """跑一次 AI 决策；失败时返回空 list（本周期不挂单）。"""
    try:
        max_n = int(cfg.get("AI_MAX_ORDERS_PER_CALL") or 20)
        # 复用 advisor 的指标计算（手动算简化版）
        closes = [r[4] for r in klines_recent]
        volumes = [r[5] for r in klines_recent]
        from orbitai.core.advisor import _rsi, _ema, _atr, _bollinger, _volume_ratio
        rsi14 = _rsi(closes, 14)
        ema20 = _ema(closes, 20)
        ema60 = _ema(closes, 60)
        atr14 = _atr(klines_recent, 14)
        bb_up, bb_mid, bb_low = _bollinger(closes, 20, 2.0)
        vol_ratio = _volume_ratio(volumes, 20)
        payload = advisor._build_prompt(
            last_px=last_px, center=last_px, drift_pct=0,
            rsi14=rsi14, ema20=ema20, ema60=ema60,
            atr14=atr14, bb_up=bb_up, bb_mid=bb_mid, bb_low=bb_low,
            vol_ratio=vol_ratio,
            klines=klines_recent[-10:], bar=bar,
        )
        raw = advisor._call_llm(payload)
        if raw is None:
            return []
        adv = advisor._parse_advice(raw)
        return [
            {"side": o.side, "open_price": o.open_price,
             "close_price": o.close_price, "order_type": o.order_type}
            for o in adv.orders
        ]
    except Exception as e:
        logger.warning(f"AI 决策异常 ({e})，本周期空挂")
        return []


def _simulate_fill(trade: Trade, k_low: float, k_high: float, ts: int) -> bool:
    """判断当前 K 线是否能完成 trade 的 open / close。返回 trade 是否完整结束。"""
    if trade.state == "open":
        if k_low <= trade.open_px <= k_high:
            trade.state = "open_filled"
            trade.open_ts = ts
    if trade.state == "open_filled":
        if k_low <= trade.close_px <= k_high:
            trade.state = "done"
            trade.close_ts = ts
            # PnL（ctVal=0.1 for ETH-USDT-SWAP；简化为 0.1）
            ct_val = 0.1
            if trade.side == "long":
                trade.pnl_gross = (trade.close_px - trade.open_px) * trade.sz * ct_val
            else:
                trade.pnl_gross = (trade.open_px - trade.close_px) * trade.sz * ct_val
            # 双边 maker 费 0.02%
            fee_rate = 0.0002 if trade.state == "done" else 0.0
            notional = (trade.open_px + trade.close_px) * trade.sz * ct_val
            trade.fee = notional * fee_rate
            return True
    return False


def run_backtest(inst_id: str, bar: str, bars: int, ai_interval_bars: int,
                 sz_ct: float = 0.01, max_orders: int = 20, dry_run: bool = False) -> dict:
    """主入口。"""
    logger.info(f"拉 {bars} 根 {bar} K 线 inst={inst_id}")
    kl = _fetch_klines(inst_id, bar, bars)
    if len(kl) < 100:
        return {"error": f"K 线数据不足 ({len(kl)} 根)"}
    logger.info(f"实际拿到 {len(kl)} 根；预热 60 根，剩余 {len(kl) - 60} 根回测")

    open_trades: list[Trade] = []
    done_trades: list[Trade] = []
    pnl_curve: list[tuple[int, float]] = []
    cum_pnl = 0.0
    cycle_count = 0

    for i in range(60, len(kl)):
        ts, o, h, l, c, v = kl[i]

        # 撮合检查所有 open trades
        still_open = []
        for tr in open_trades:
            done = _simulate_fill(tr, l, h, ts)
            if tr.state == "done":
                done_trades.append(tr)
                cum_pnl += tr.pnl_net
            elif tr.state in ("open", "open_filled"):
                still_open.append(tr)
        open_trades = still_open
        pnl_curve.append((ts, round(cum_pnl, 4)))

        # 每 ai_interval_bars 根重新决策一次
        if (i - 60) % ai_interval_bars == 0:
            cycle_count += 1
            window = kl[max(0, i - 60):i + 1]
            last_px = c
            if dry_run:
                orders = _build_ladder_dry(last_px, max_orders)
            else:
                orders = _ai_decide(window, last_px, bar)
            # 旧未成交 open 单视为撤销（模拟现实 AI 模式）
            cancelled = sum(1 for tr in open_trades if tr.state == "open")
            open_trades = [tr for tr in open_trades if tr.state != "open"]
            # 注入新订单
            for od in orders[:max_orders]:
                open_trades.append(Trade(
                    side=od["side"],
                    open_px=od["open_price"],
                    close_px=od["close_price"],
                    sz=sz_ct,
                ))

    n_done = len(done_trades)
    n_unfilled = len(open_trades)
    wins = sum(1 for t in done_trades if t.pnl_net > 0)
    losses = sum(1 for t in done_trades if t.pnl_net <= 0)
    fee_sum = sum(t.fee for t in done_trades)

    return {
        "inst_id": inst_id,
        "bar": bar,
        "bars": len(kl),
        "ai_cycles": cycle_count,
        "trades_done": n_done,
        "trades_unfilled": n_unfilled,
        "win_rate": round(wins / max(1, n_done), 3),
        "wins": wins, "losses": losses,
        "cum_pnl": round(cum_pnl, 4),
        "fee_sum": round(fee_sum, 4),
        "avg_pnl_per_trade": round(cum_pnl / max(1, n_done), 4),
        "pnl_curve": pnl_curve,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orbitai-backtest")
    p.add_argument("--inst", default=None, help="标的，默认 INST_ID 配置")
    p.add_argument("--bar", default="1m", help="K 线周期")
    p.add_argument("--bars", type=int, default=1000, help="拉取 K 线根数")
    p.add_argument("--ai-interval-bars", type=int, default=1,
                   help="每多少根 K 线触发一次 AI 决策（dry-run 模式建议 1）")
    p.add_argument("--sz", type=float, default=0.01, help="模拟每对张数")
    p.add_argument("--max-orders", type=int, default=20)
    p.add_argument("--dry-run", action="store_true",
                   help="不调 LLM，用固定 ladder 验证撮合引擎")
    p.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = p.parse_args(argv)

    inst_id = args.inst or (cfg.get("INST_ID") or "ETH-USDT-SWAP")
    result = run_backtest(
        inst_id=inst_id, bar=args.bar, bars=args.bars,
        ai_interval_bars=args.ai_interval_bars,
        sz_ct=args.sz, max_orders=args.max_orders,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if "error" in result:
        logger.error(result["error"])
        return 1
    r = result
    print(f"\n=== 回测结果 {r['inst_id']} {r['bar']} ===")
    print(f"  K 线数:        {r['bars']}")
    print(f"  AI 决策次数:   {r['ai_cycles']}")
    print(f"  完成对数:      {r['trades_done']}")
    print(f"  未成交对数:    {r['trades_unfilled']}")
    print(f"  胜率:          {r['win_rate'] * 100:.1f}% ({r['wins']}/{r['wins']+r['losses']})")
    print(f"  累计净利:      {r['cum_pnl']:+.4f} USDT")
    print(f"  累计手续费:    {r['fee_sum']:.4f} USDT")
    print(f"  单对均利:      {r['avg_pnl_per_trade']:+.4f} USDT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
