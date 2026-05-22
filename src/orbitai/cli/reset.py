# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""一键重置:撤所有挂单 + 市价平所有持仓 + 清空 grid.db

用法:
    .venv/bin/python reset.py

重置后可以用 main.py 重新部署网格。
trades 表保留作为历史。
"""
import time
from loguru import logger
from orbitai.core.grid import cancel_all_orders, close_all_positions
from orbitai.data.client import account_api
from orbitai.config import defaults as config
from orbitai.data import db as db


def main():
    logger.warning("=== 重置流程 开始 ===")

    # 1) 撤销 db 中记录的挂单
    cancel_all_orders()
    time.sleep(1)

    # 2) 兜底:再用 OKX 接口拉一遍未成交单,把可能在 db 之外的也撤了
    from client import trade_api
    pending = trade_api().get_order_list(instType="SWAP", instId=config.INST_ID)
    if pending.get("code") == "0":
        leftover = pending.get("data", [])
        if leftover:
            logger.warning(f"发现 {len(leftover)} 笔 db 外残留挂单,撤销")
            for o in leftover:
                trade_api().cancel_order(instId=config.INST_ID, ordId=o["ordId"])
                time.sleep(0.05)
        else:
            logger.info("OKX 端无残留挂单")

    # 3) 平所有持仓
    logger.info("平所有持仓 ...")
    close_all_positions()
    time.sleep(1)

    # 4) 二次确认持仓清空
    pos = account_api().get_positions(instType="SWAP", instId=config.INST_ID)
    if pos.get("code") == "0":
        nonzero = [p for p in pos["data"] if abs(float(p.get("pos", 0))) > 1e-9]
        if nonzero:
            logger.error(f"⚠️ 仍有 {len(nonzero)} 笔持仓未清,请到 OKX 网页手动处理:")
            for p in nonzero:
                logger.error(f"  posSide={p['posSide']}  pos={p['pos']}")
        else:
            logger.success("持仓已清空")

    # 5) 清 db
    db.clear_all()
    logger.success("grid.db 已重置(trades 历史保留)")
    logger.warning("=== 重置完成,可以重新跑 main.py ===")


if __name__ == "__main__":
    main()
