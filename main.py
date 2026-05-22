# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""网格策略启动入口

用法:
    cd ~/Develop/okx-bot
    .venv/bin/python main.py

第一次启动:挂初始网格 + 进入主循环
重启场景:检测到 db 已有状态 → 启动对账(与 OKX pending 单/持仓比对)→ 进入主循环

Ctrl+C 退出会撤销所有挂单(持仓保留,下次启动会对账接管)。
完全重置:rm grid.db  或  跑 reset.py(撤单 + 平仓 + 清 db)。
"""
import atexit
import os
import signal
import sys
import traceback
from loguru import logger
import grid
from grid import (
    initialize, main_loop, reconcile_on_startup,
    _load_instrument, _ensure_position_mode, _ensure_leverage, _get_last_price,
)
import db
import config
import config_loader
import notify

PID_FILE = os.path.join(os.path.dirname(__file__), "bot.pid")


def _write_pid() -> None:
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_remove_pid)
    # 启动阶段(ai_main_loop 之前)收到停止信号直接 sys.exit 触发 atexit 清 pid;
    # 进入 ai_main_loop 后 grid.py 会注册自己的 handler 接管,做撤单清理。
    handler = lambda *_: sys.exit(0)
    signal.signal(signal.SIGTERM, handler)
    # Windows 收不到 SIGTERM，webui 改发 CTRL_BREAK_EVENT (= SIGBREAK)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)


def _remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def setup_logging():
    """加文件归档,按天分,留 30 天"""
    os.makedirs("logs", exist_ok=True)
    logger.add(
        "logs/grid_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )


def main():
    setup_logging()
    _write_pid()

    _load_instrument()
    _ensure_position_mode()
    _ensure_leverage()

    if config_loader.get("AI_DRIVEN_MODE"):
        logger.info("=== OKX AI 驱动模式 启动 ===")
        logger.info(f"标的={config.INST_ID}  资金={config.TOTAL_USDT}U  杠杆={config.LEVERAGE}x  "
                    f"AI 间隔={config.AI_INTERVAL_SEC}s  最大订单对={config.AI_MAX_ORDERS_PER_CALL}")
        # AI 驱动模式与机械网格 DB schema 互斥;如果检测到旧 grids 表有数据,提醒先 reset
        if db.all_grids():
            logger.error(
                "检测到旧的机械网格状态 (grids 表非空)。"
                "AI 驱动模式与机械网格不兼容,请先运行 reset.py 或手动 rm grid.db 后再启动。"
            )
            sys.exit(2)
        db.init_db()
        grid.ai_main_loop()
        return

    logger.info("=== OKX 中性网格(机械)启动 ===")
    logger.info(f"标的={config.INST_ID}  资金={config.TOTAL_USDT}U  杠杆={config.LEVERAGE}x  "
                f"格数={config.GRID_COUNT}  区间±{config.RANGE_PCT*100:.0f}%  "
                f"止损±{config.STOP_LOSS_PCT*100:.0f}%")
    db.init_db()
    if not db.all_grids():
        center = _get_last_price()
        initialize(center)
    else:
        center = float(db.get_meta("center_price"))
        logger.info(f"检测到已有网格状态(中心价 {center}),进入对账")
        reconcile_on_startup()
    main_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("主线程异常退出")
        notify.alert(f"主线程异常退出: {e}\n```\n{tb[-1500:]}\n```",
                     dedup_key="main_crash")
        sys.exit(1)
