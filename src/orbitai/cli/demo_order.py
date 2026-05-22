# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""下单链路演示

流程:
  1) 查 ETH-USDT-SWAP 最新价
  2) 查合约规格(最小张数、张数面值),用于计算 sz
  3) 挂一笔远离当前价 30% 的限价买单(绝不会成交)
  4) 查询该订单状态
  5) 撤销该订单
  6) 再查一次,确认状态变为 canceled

跑通这个脚本 = 走通了 OKX 全部 REST 交易链路,为后面的网格策略打基础。

注意:
  - 默认在模拟盘跑(OKX_SIMULATED=1),实盘前请把单量调得更小或换合约
  - 永续合约用张数(sz)计价,1 张 ETH-USDT-SWAP = 0.1 ETH,
    所以最小下单 1 张 = 0.1 ETH,价格 2000 USDT 时面值约 200 USDT
"""
import time
import sys
from loguru import logger
from orbitai.data.client import market_api, public_api, trade_api, is_simulated

INST_ID = "ETH-USDT-SWAP"
TD_MODE = "cross"   # 全仓保证金;网格用全仓更稳,逐仓爆单只是单合约
SIDE = "buy"
POS_SIDE = "long"   # 双向持仓模式下买入开多;如果你账户是单向持仓模式这个字段会被忽略
SZ = "1"            # 1 张 = 0.1 ETH
PX_DISCOUNT = 0.7   # 挂在当前价 70% 的位置,不会成交

logger.info(f"模拟盘={is_simulated()}  交易对={INST_ID}")

# 1) 最新价
mkt = market_api()
t = mkt.get_ticker(instId=INST_ID)
if t.get("code") != "0":
    logger.error(f"取行情失败: {t}")
    sys.exit(1)
last_px = float(t["data"][0]["last"])
logger.info(f"当前价: {last_px}")

# 2) 合约规格(只是打印参考,不影响下单)
pub = public_api()
spec = pub.get_instruments(instType="SWAP", instId=INST_ID)
if spec.get("code") == "0" and spec["data"]:
    s = spec["data"][0]
    logger.info(f"合约规格: ctVal={s.get('ctVal')} (每张面值)  minSz={s.get('minSz')} (最小张数)  lotSz={s.get('lotSz')}")

# 3) 挂远离当前价的限价买单
target_px = round(last_px * PX_DISCOUNT, 2)
logger.info(f"挂限价买单: px={target_px}  sz={SZ} 张 (当前价 {PX_DISCOUNT * 100:.0f}%)")

trade = trade_api()
order = trade.place_order(
    instId=INST_ID,
    tdMode=TD_MODE,
    side=SIDE,
    posSide=POS_SIDE,
    ordType="limit",
    px=str(target_px),
    sz=SZ,
)
if order.get("code") != "0":
    logger.error(f"下单失败: {order}")
    # 常见错误:
    #   51000  参数错误(检查 px/sz 格式、合约规格)
    #   51008  保证金不足(模拟盘没领资金)
    #   51169  持仓方式与请求不符(账户配置成单向/双向影响 posSide)
    sys.exit(2)

ord_id = order["data"][0]["ordId"]
logger.success(f"下单成功: ordId={ord_id}")

# 4) 查单
time.sleep(0.5)
got = trade.get_order(instId=INST_ID, ordId=ord_id)
if got.get("code") == "0":
    d = got["data"][0]
    logger.info(f"查单: state={d['state']}  px={d['px']}  sz={d['sz']}  filled={d['fillSz']}")
else:
    logger.warning(f"查单异常: {got}")

# 5) 撤单
cancel = trade.cancel_order(instId=INST_ID, ordId=ord_id)
if cancel.get("code") != "0":
    logger.error(f"撤单失败: {cancel}")
    sys.exit(3)
logger.success(f"撤单成功: ordId={ord_id}")

# 6) 再查一次,确认状态
time.sleep(0.5)
final = trade.get_order(instId=INST_ID, ordId=ord_id)
if final.get("code") == "0":
    d = final["data"][0]
    logger.success(f"最终状态: state={d['state']}  (预期 canceled)")
else:
    logger.warning(f"二次查单异常: {final}")

logger.success("✅ 下单/查单/撤单链路全通")
