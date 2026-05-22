# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""告警 —— 仅写本地日志（loguru）

历史版本会把同一条消息推到企业微信群机器人，但 macOS Python 默认
不带 CA 根证书导致 SSL 验证失败。改为纯日志：

  - info  → logger.info
  - warn  → logger.warning
  - alert → logger.error

仍保留 dedup_key 去重（同一 key 60 秒内只打一次），避免主循环刷屏。
"""
import time
from loguru import logger

_DEDUP_WINDOW = 60      # 同一 dedup_key 去重窗口（秒）
_last_sent: dict[str, float] = {}


def _should_emit(content: str, dedup_key: str | None) -> bool:
    key = dedup_key or content
    now = time.time()
    last = _last_sent.get(key, 0)
    if now - last < _DEDUP_WINDOW:
        return False
    _last_sent[key] = now
    return True


def info(content: str, dedup_key: str | None = None):
    """普通通知（策略启动 / 暂停等里程碑事件）"""
    if _should_emit(content, dedup_key):
        logger.info(f"[NOTIFY] {content}")


def warn(content: str, dedup_key: str | None = None):
    """警告（保证金低、连续超时等）"""
    if _should_emit(content, dedup_key):
        logger.warning(f"[NOTIFY] {content}")


def alert(content: str, dedup_key: str | None = None):
    """严重告警（止损触发、程序异常退出等）"""
    if _should_emit(content, dedup_key):
        logger.error(f"[NOTIFY] {content}")
