# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""AI 网格顾问（DeepSeek）

定期（默认 5 分钟）拉一段 K 线 + 算 RSI/EMA，把状态喂给 DeepSeek，
让它输出当前应该「允许哪个方向开仓」+「是否建议重居中」。

主线程通过 `current_advice()` / `is_open_allowed(direction)` 同步读取最新建议，
绝不阻塞交易主循环。

DeepSeek 不可用、key 没配、解析失败、网络抖动 —— 全部退化为「双向放行 + 不重居中」，
保证主流程绝不卡壳。
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

import config
import config_loader
from client import market_api
import db

load_dotenv()

import llm_keys

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


def _current_provider() -> dict:
    """返回当前选中 provider 的元数据 + key。"""
    name = (config_loader.get("AI_PROVIDER") or "deepseek").lower()
    meta = llm_keys.PROVIDERS.get(name)
    if meta is None:
        # 配错回退到 deepseek
        name = "deepseek"
        meta = llm_keys.PROVIDERS["deepseek"]
    return {
        "name": name,
        "url": meta["url"],
        "style": meta["style"],
        "default_model": meta["model"],
        "key": llm_keys.get_key(name),
    }

# Prompt 模板从文件加载，UI 可以热修改。文件位置 prompts/scalp.txt
PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompts", "scalp.txt")


def _load_prompt_template() -> str:
    try:
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        logger.warning(f"[AI] 读取 prompt 文件失败 {PROMPT_FILE}: {e}")
        return ""


# ============================================================
# Advice 数据模型
# ============================================================
@dataclass
class AIOrder:
    """AI 驱动模式下的一对 open/close 订单。"""
    side: str                 # 'long' | 'short'
    open_price: float
    close_price: float
    order_type: str = "limit"  # 'limit'(默认,挂单等价) | 'market'(立即成交,追突破)


@dataclass
class Advice:
    trend: str = "sideways"          # up / down / sideways
    confidence: float = 0.0          # 0..1
    long_allowed: bool = True
    short_allowed: bool = True
    recenter_to: Optional[float] = None  # AI 建议的新中心价；None=不建议
    orders: list[AIOrder] = field(default_factory=list)  # AI 驱动模式下的订单建议
    reason: str = ""
    fetched_at: float = field(default_factory=time.time)
    seq: int = 0                     # 单调递增,主循环用来识别「新一份建议来了」

    def age_sec(self) -> float:
        return time.time() - self.fetched_at

    @property
    def is_stale(self) -> bool:
        # 超过两倍间隔视为过期，自动降级到「双向放行」
        return self.age_sec() > config_loader.get("AI_INTERVAL_SEC") * 2


# 默认放行的建议，AI 未启用 / 调用失败时返回
_DEFAULT_ADVICE = Advice(trend="sideways", confidence=0.0,
                         long_allowed=True, short_allowed=True,
                         recenter_to=None, reason="default-allow-all")

_lock = threading.Lock()
_current: Advice = _DEFAULT_ADVICE
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_seq_counter = 0


# ============================================================
# 公共接口（线程安全）
# ============================================================
def current_advice() -> Advice:
    with _lock:
        adv = _current
    if adv.is_stale:
        # 过期 → 降级。返回新对象避免误改
        return Advice(reason=f"stale({int(adv.age_sec())}s ago)")
    return adv


def is_open_allowed(direction: str) -> bool:
    """direction: 'long' | 'short'。AI 未启用或建议过期时一律放行。"""
    if not _is_enabled():
        return True
    adv = current_advice()
    if direction == "long":
        return adv.long_allowed
    if direction == "short":
        return adv.short_allowed
    return True


def _is_enabled() -> bool:
    if not config_loader.get("AI_ENABLED"):
        return False
    return bool(_current_provider()["key"])


# ============================================================
# 启停
# ============================================================
def start():
    global _thread
    if not _is_enabled():
        logger.info("AI 顾问未启用（AI_ENABLED=False 或当前 provider 缺 API key），跳过启动")
        return
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="ai-advisor", daemon=True)
    _thread.start()
    logger.info(f"AI 顾问线程已启动；每 {config_loader.get('AI_INTERVAL_SEC')}s 刷新一次（{config_loader.get('AI_MODEL')}）")


def stop():
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)


# ============================================================
# 后台线程
# ============================================================
def _loop():
    global _current, _seq_counter
    # 启动即拉一次，不等首个间隔
    while not _stop_event.is_set():
        try:
            advice = _refresh_once()
            _seq_counter += 1
            advice.seq = _seq_counter
            with _lock:
                _current = advice
            order_brief = ""
            if advice.orders:
                order_brief = " orders=" + ",".join(
                    f"{o.side}({o.open_price:.2f}→{o.close_price:.2f})"
                    for o in advice.orders[:6]
                )
            logger.info(f"[AI seq={advice.seq}] trend={advice.trend} conf={advice.confidence:.2f} "
                        f"long={'Y' if advice.long_allowed else 'N'} "
                        f"short={'Y' if advice.short_allowed else 'N'} "
                        f"recenter={advice.recenter_to}{order_brief} | {advice.reason[:80]}")
        except Exception:
            logger.exception("[AI] 刷新失败，本轮保持上一份建议")
        # 可中断的 sleep
        if _stop_event.wait(timeout=config_loader.get("AI_INTERVAL_SEC")):
            break


def _refresh_once() -> Advice:
    klines = _fetch_klines(config_loader.get("AI_KLINE_BAR"), config_loader.get("AI_KLINE_LIMIT"))
    if not klines:
        return Advice(reason="kline-fetch-failed")
    closes = [row[4] for row in klines]
    volumes = [row[5] for row in klines]
    rsi14 = _rsi(closes, 14)
    ema20 = _ema(closes, 20)
    ema60 = _ema(closes, 60)
    atr14 = _atr(klines, 14)
    bb_up, bb_mid, bb_low = _bollinger(closes, 20, 2.0)
    vol_ratio = _volume_ratio(volumes, 20)
    last_px = closes[-1]
    center = float(db.get_meta("center_price") or last_px)
    drift_pct = (last_px - center) / center

    prompt_payload = _build_prompt(
        last_px=last_px, center=center, drift_pct=drift_pct,
        rsi14=rsi14, ema20=ema20, ema60=ema60,
        atr14=atr14, bb_up=bb_up, bb_mid=bb_mid, bb_low=bb_low,
        vol_ratio=vol_ratio,
        klines=klines[-10:],   # 只把最近 10 根 OHLC 喂给模型，节省 token
        bar=config_loader.get("AI_KLINE_BAR"),
    )
    raw = _call_llm(prompt_payload)
    if raw is None:
        return Advice(reason="llm-unreachable")
    return _parse_advice(raw)


# ============================================================
# 行情 & 指标
# ============================================================
def _fetch_klines(bar: str, limit: int) -> list[tuple[int, float, float, float, float, float]]:
    """返回 [(ts, open, high, low, close, volume), ...] 按时间升序。失败返回空 list。
    volume 取 OKX 的 vol 列（基础币种成交量，对 ETH-USDT-SWAP 即 ETH 数量）。
    """
    try:
        r = market_api().get_candlesticks(instId=config_loader.get("INST_ID"), bar=bar, limit=str(limit))
    except Exception as e:
        logger.warning(f"[AI] 拉 K 线异常: {e}")
        return []
    if r.get("code") != "0":
        logger.warning(f"[AI] 拉 K 线响应非 0: {r}")
        return []
    rows = []
    # OKX 返回的是 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]，按时间倒序
    for row in r.get("data", []):
        try:
            rows.append((int(row[0]), float(row[1]), float(row[2]),
                         float(row[3]), float(row[4]), float(row[5])))
        except (ValueError, IndexError):
            continue
    rows.sort(key=lambda x: x[0])
    return rows


def _rsi(values: list[float], period: int = 14) -> float:
    """Wilder's RSI。数据不够返回 50（中性）。"""
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for px in values[period:]:
        ema = px * k + ema * (1 - k)
    return ema


def _atr(klines: list[tuple], period: int = 14) -> float:
    """Wilder's ATR(period). klines 每行 = (ts, o, h, l, c, vol)。
    返回 0 当数据不足。
    """
    if len(klines) < period + 1:
        return 0.0
    # True Range: max(H-L, |H-prevC|, |L-prevC|)
    trs: list[float] = []
    prev_c = klines[0][4]
    for row in klines[1:]:
        h, l, c = row[2], row[3], row[4]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
    if len(trs) < period:
        return sum(trs) / len(trs)
    # 初始 ATR = 前 period 个 TR 均值
    atr = sum(trs[:period]) / period
    # 之后 Wilder smoothing
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _bollinger(closes: list[float], period: int = 20, sigma: float = 2.0) -> tuple[float, float, float]:
    """返回 (upper, middle, lower)。数据不足返回 (0, 0, 0)。"""
    if len(closes) < period:
        return 0.0, 0.0, 0.0
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    return mid + sigma * std, mid, mid - sigma * std


def _volume_ratio(volumes: list[float], period: int = 20) -> float:
    """当前 K 线成交量 / 之前 (period-1) 根均量。无数据返回 1.0。"""
    if len(volumes) < period:
        return 1.0
    current = volumes[-1]
    prior = volumes[-period:-1]
    avg = sum(prior) / len(prior) if prior else 0
    if avg <= 0:
        return 1.0
    return current / avg


# ============================================================
# DeepSeek 调用
# ============================================================
def _build_prompt(*, last_px, center, drift_pct, rsi14, ema20, ema60,
                  atr14=0.0, bb_up=0.0, bb_mid=0.0, bb_low=0.0, vol_ratio=1.0,
                  klines, bar) -> dict:
    kl_lines_text = "\n".join(
        f"{ts}: O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f} V={v:.2f}"
        for ts, o, h, l, c, v in klines
    )
    # 派生信号:布林带相对位置 + 带宽
    bb_pct = ((last_px - bb_low) / (bb_up - bb_low) * 100) if bb_up > bb_low else 50.0
    bb_bw = ((bb_up - bb_low) / bb_mid * 100) if bb_mid > 0 else 0.0
    atr_pct = (atr14 / last_px * 100) if last_px > 0 else 0.0
    if config_loader.get("AI_DRIVEN_MODE"):
        # AI 完全驱动:从文件加载 prompt 模板,UI 可热修改
        max_n = config_loader.get("AI_MAX_ORDERS_PER_CALL")
        template = _load_prompt_template()
        user_msg = template.format(
            inst_id=config_loader.get("INST_ID"),
            bar=bar,
            last_px=last_px,
            rsi14=rsi14, ema20=ema20, ema60=ema60,
            atr14=atr14, atr_pct=atr_pct,
            bb_up=bb_up, bb_mid=bb_mid, bb_low=bb_low,
            bb_pct=bb_pct, bb_bw=bb_bw,
            vol_ratio=vol_ratio,
            kl_lines=kl_lines_text,
            max_n=max_n,
        )
        max_tokens = 1500
    else:
        # 非 AI 驱动模式(legacy):内嵌简单趋势判断 prompt
        user_msg = f"""你是加密合约网格交易顾问，对 {config.INST_ID} 进行 {bar} 级别趋势判断。

【当前网格状态】
- 当前价: {last_px:.2f}
- 网格中心价: {center:.2f}
- 偏离中心: {drift_pct*100:+.2f}%
- 区间: 中心 ±{config.RANGE_PCT*100:.0f}%

【技术指标】
- RSI(14) = {rsi14:.1f}
- EMA(20) = {ema20:.2f}
- EMA(60) = {ema60:.2f}

【最近 K 线（{bar}）】
{kl_lines_text}

【你的任务】
给出对未来 30-60 分钟的趋势判断，并决定网格是否暂停某个方向开仓 / 是否需要重居中。

判断准则：
- 上涨趋势：暂停做空开仓（防被套），多头继续；若偏离 >10% 且趋势确认，建议重居中到当前价附近
- 下跌趋势：暂停做多开仓，空头继续；同上
- 震荡：双向放行，不重居中

【输出格式】严格 JSON，禁止 markdown 包裹：
{{"trend":"up|down|sideways","confidence":0.0-1.0,"long_allowed":true|false,"short_allowed":true|false,"recenter_to":null|<float>,"reason":"<不超过 100 字的判断依据>"}}"""
        max_tokens = 400

    return {
        "model": config_loader.get("AI_MODEL"),
        "messages": [
            {"role": "system", "content": "你是严谨的量化分析助手，只输出 JSON。"},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }


# SSL 上下文:
#  - config.AI_VERIFY_SSL=True  → 严格校验,失败时一次性降级并打 INFO(MITM 代理常见)
#  - config.AI_VERIFY_SSL=False → 直接用 unverified,免去无谓的 fallback warning
_ssl_insecure = not config_loader.get("AI_VERIFY_SSL")


def _http_post(url: str, body: dict, headers: dict, tag: str) -> Optional[str]:
    """通用 POST，处理 MITM 代理自签证书 fallback。"""
    global _ssl_insecure
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    def _open(ctx):
        return urllib.request.urlopen(req, timeout=config_loader.get("AI_REQUEST_TIMEOUT"), context=ctx)

    ctx = ssl._create_unverified_context() if _ssl_insecure else ssl.create_default_context()
    try:
        with _open(ctx) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        logger.warning(f"[AI] {tag} HTTP {e.code}: {msg}")
        return None
    except urllib.error.URLError as e:
        if not _ssl_insecure and isinstance(e.reason, ssl.SSLError):
            logger.info(f"[AI] {tag} 检测到 MITM 代理(自签证书),后续请求跳过 TLS 校验")
            _ssl_insecure = True
            try:
                with _open(ssl._create_unverified_context()) as resp:
                    return resp.read().decode("utf-8")
            except Exception as e2:
                logger.warning(f"[AI] {tag} 仍不可达: {e2}")
                return None
        logger.warning(f"[AI] {tag} 网络异常: {e}")
        return None
    except (TimeoutError, OSError) as e:
        logger.warning(f"[AI] {tag} 网络异常: {e}")
        return None


def _call_llm(payload: dict) -> Optional[str]:
    """根据 AI_PROVIDER 把统一 payload(OpenAI 风格)翻译并发出。"""
    prov = _current_provider()
    if not prov["key"]:
        logger.warning(f"[AI] provider={prov['name']} 未配置 API key,跳过本轮决策")
        return None

    model = payload.get("model") or prov["default_model"]
    system_msg = ""
    user_msg = ""
    for m in payload.get("messages", []):
        if m.get("role") == "system":
            system_msg = m.get("content", "")
        elif m.get("role") == "user":
            user_msg = m.get("content", "")
    max_tokens = payload.get("max_tokens", 1500)
    temperature = payload.get("temperature", 0.1)
    style = prov["style"]
    tag = prov["name"]

    if style == "openai":
        body = {
            "model": model,
            "messages": payload["messages"],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        url = prov["url"]
        headers = {
            "Authorization": f"Bearer {prov['key']}",
            "Content-Type": "application/json",
        }
    elif style == "anthropic":
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_msg,
            "messages": [{"role": "user", "content": user_msg}],
        }
        url = prov["url"]
        headers = {
            "x-api-key": prov["key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    elif style == "gemini":
        combined = (system_msg + "\n\n" + user_msg) if system_msg else user_msg
        body = {
            "contents": [{"role": "user", "parts": [{"text": combined}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        url = prov["url"].format(model=model) + f"?key={prov['key']}"
        headers = {"Content-Type": "application/json"}
    else:
        logger.error(f"[AI] 未知 provider style: {style}")
        return None

    raw = _http_post(url, body, headers, tag)
    if raw is None:
        return None

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[AI] {tag} 响应非 JSON: {e} | body={raw[:200]}")
        return None

    try:
        if style == "openai":
            return obj["choices"][0]["message"]["content"]
        elif style == "anthropic":
            # content = [{"type":"text","text":"..."}, ...] 取所有 text 拼起来
            parts = [c.get("text", "") for c in obj.get("content", []) if c.get("type") == "text"]
            return "".join(parts) if parts else None
        elif style == "gemini":
            parts = obj["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as e:
        logger.warning(f"[AI] {tag} 响应字段缺失: {e} | body={raw[:300]}")
        return None


def _parse_advice(raw: str) -> Advice:
    try:
        # 容错：极少数模型还会包 ```json```，剥掉
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:].lstrip()
        obj = json.loads(s)
        trend = str(obj.get("trend", "sideways")).lower()
        if trend not in ("up", "down", "sideways"):
            trend = "sideways"
        recenter = obj.get("recenter_to")
        if recenter is not None:
            try:
                recenter = float(recenter)
            except (TypeError, ValueError):
                recenter = None
        orders: list[AIOrder] = []
        for o in obj.get("orders", []) or []:
            try:
                side = str(o.get("side", "")).lower()
                if side not in ("long", "short"):
                    continue
                op = float(o["open_price"])
                cp = float(o["close_price"])
                otype = str(o.get("order_type", "limit")).lower()
                if otype not in ("limit", "market"):
                    otype = "limit"
                orders.append(AIOrder(side=side, open_price=op, close_price=cp, order_type=otype))
            except (KeyError, TypeError, ValueError):
                continue
        max_n = config_loader.get("AI_MAX_ORDERS_PER_CALL")
        if len(orders) > max_n:
            logger.warning(f"[AI] 返回 {len(orders)} 对超出上限,只取前 {max_n} 对")
            orders = orders[:max_n]
        # 安全阀:market 单不能超过 30%,超出的强制改回 limit
        max_market = max(1, len(orders) * 30 // 100)
        market_cnt = 0
        for o in orders:
            if o.order_type == "market":
                if market_cnt >= max_market:
                    o.order_type = "limit"
                else:
                    market_cnt += 1
        # 非 AI 驱动模式下保留 long_allowed/short_allowed 默认行为
        long_allowed = bool(obj.get("long_allowed", True))
        short_allowed = bool(obj.get("short_allowed", True))
        return Advice(
            trend=trend,
            confidence=float(obj.get("confidence", 0.0) or 0.0),
            long_allowed=long_allowed,
            short_allowed=short_allowed,
            recenter_to=recenter,
            orders=orders,
            reason=str(obj.get("reason", ""))[:200],
        )
    except Exception as e:
        logger.warning(f"[AI] 解析 JSON 失败 raw={raw[:200]!r}: {e}")
        return Advice(reason="parse-failed")
