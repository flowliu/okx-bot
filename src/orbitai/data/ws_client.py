# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""OKX private WebSocket 客户端：订阅 orders 频道，把订单状态推送到进程级
thread-safe cache。grid.py 主循环优先读 cache，cache 没就绪时 fallback REST。

设计：
- python-okx 的 WsPrivateAsync 跑在独立 daemon 线程的 asyncio loop 里
- 推送进 _pending dict，主线程加锁读
- _ws_ready 事件标志：WS 已 login + 订阅成功 + 至少收过一次数据
- 启动失败 / 断线 → 静默退出（_ws_ready 永不 set）→ 主循环 fallback REST
"""
import asyncio
import json
import threading
import time
from typing import Optional

from loguru import logger

from orbitai.config import loader as cfg
from orbitai.data.client import API_KEY, SECRET_KEY, PASSPHRASE, is_simulated

# 不同区域对应的 private WS URL
_WS_URLS = {
    "https://www.okx.com": "wss://ws.okx.com:8443/ws/v5/private",
    "https://aws.okx.com": "wss://wsaws.okx.com:8443/ws/v5/private",
    "https://us.okx.com":  "wss://us.okx.com:8443/ws/v5/private",
    "https://eea.okx.com": "wss://eea.okx.com:8443/ws/v5/private",
}
_WS_SIM = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"

# 进程级 cache：ordId → 订单数据（OKX 推送的 raw item）
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()

# 状态标志
_started = threading.Event()
_ws_ready = threading.Event()
_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def get_pending() -> Optional[dict[str, dict]]:
    """主循环调用。返回当前缓存的 pending 单 dict，若 WS 尚未就绪返回 None。"""
    if not _ws_ready.is_set():
        return None
    with _pending_lock:
        return dict(_pending)


def is_ready() -> bool:
    return _ws_ready.is_set()


def is_started() -> bool:
    return _started.is_set()


async def _consume(ws, on_msg) -> None:
    """SDK 没有内置 consume loop，自己跑：从 ws.websocket.recv() 读消息。"""
    while not _stop_event.is_set():
        try:
            raw = await asyncio.wait_for(ws.websocket.recv(), timeout=30)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.warning(f"[WS] recv 异常: {e}")
            break
        try:
            on_msg(raw)
        except Exception as e:
            logger.warning(f"[WS] 回调异常: {e}")


def _handle_message(raw) -> None:
    """处理一条 OKX 推送。订阅响应 / 心跳 / 数据 三类。"""
    try:
        msg = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return
    if not isinstance(msg, dict):
        return
    # 订阅响应 / 错误
    if "event" in msg:
        ev = msg.get("event")
        if ev == "subscribe":
            _ws_ready.set()
            logger.info(f"[WS] 订阅成功: {msg.get('arg')}")
        elif ev == "error":
            logger.warning(f"[WS] 错误响应: {msg}")
        return
    # 数据推送
    data = msg.get("data") or []
    if not data:
        return
    with _pending_lock:
        for item in data:
            ord_id = item.get("ordId", "")
            state = item.get("state", "")
            if not ord_id:
                continue
            # OKX 订单状态: live / partially_filled / filled / canceled / mmp_canceled
            if state in ("live", "partially_filled"):
                _pending[ord_id] = item
            else:
                # filled / canceled 等 → 从 cache 移除
                _pending.pop(ord_id, None)
    # 第一次收到数据视为就绪（防御：万一 subscribe 响应没标记）
    _ws_ready.set()


async def _ws_main(inst_id: str) -> None:
    from okx.websocket.WsPrivateAsync import WsPrivateAsync

    if is_simulated():
        url = _WS_SIM
    else:
        domain = cfg.get("OKX_DOMAIN") or "https://www.okx.com"
        url = _WS_URLS.get(domain, _WS_URLS["https://www.okx.com"])

    logger.info(f"[WS] 连接 {url}")
    ws = WsPrivateAsync(API_KEY, PASSPHRASE, SECRET_KEY, url)
    try:
        await ws.start()
    except Exception as e:
        logger.warning(f"[WS] 启动失败 ({e})：主循环 fallback 到 REST 轮询")
        return

    try:
        await ws.subscribe(
            [{"channel": "orders", "instType": "SWAP", "instId": inst_id}],
            _handle_message,
        )
        await _consume(ws, _handle_message)
    except Exception as e:
        logger.warning(f"[WS] 订阅/消费异常: {e}")
    finally:
        try:
            await ws.stop()
        except Exception:
            pass
        _ws_ready.clear()
        logger.info("[WS] 已断开")


def start(inst_id: str) -> None:
    """启动 daemon 线程跑 WS。幂等。"""
    global _thread
    if _started.is_set():
        return
    if not (API_KEY and SECRET_KEY and PASSPHRASE):
        logger.warning("[WS] 缺少 OKX 凭证，跳过 WS 订阅，回退到 REST 轮询")
        return
    _started.set()
    _stop_event.clear()

    def runner():
        try:
            asyncio.run(_ws_main(inst_id))
        except Exception as e:
            logger.warning(f"[WS] 线程异常退出: {e}")
        finally:
            _ws_ready.clear()
            _started.clear()

    _thread = threading.Thread(target=runner, daemon=True, name="okx-ws-private")
    _thread.start()


def stop(timeout: float = 3.0) -> None:
    _stop_event.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=timeout)
    _ws_ready.clear()
    _started.clear()
