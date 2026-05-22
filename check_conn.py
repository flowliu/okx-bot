# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""OKX 连通性测试

跑这个脚本验证三件事:
  1) .env 里的 key/secret/passphrase 配对正确(签名能通过)
  2) 模拟盘开关 OKX_SIMULATED 设置正确
  3) 行情接口和账户接口都能调通

用法:
    cd ~/Develop/okx-bot
    .venv/bin/python check_conn.py
"""
import os
import sys
from dotenv import load_dotenv
from loguru import logger
from okx.Account import AccountAPI
from okx.MarketData import MarketAPI

load_dotenv()

API_KEY = os.getenv("OKX_API_KEY", "").strip()
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()
SIMULATED = os.getenv("OKX_SIMULATED", "1").strip()  # "1" 模拟盘, "0" 实盘

if not all([API_KEY, SECRET_KEY, PASSPHRASE]):
    logger.error("缺少 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE,请先填 .env")
    sys.exit(1)

mode = "模拟盘" if SIMULATED == "1" else "实盘"
logger.info(f"当前模式: {mode}  (OKX_SIMULATED={SIMULATED})")

# python-okx 的 flag 参数: "0"=实盘 "1"=模拟盘
flag = SIMULATED

# 1) 公共行情(无需鉴权) —— ETH-USDT-SWAP 最新价
market = MarketAPI(flag=flag)
ticker_resp = market.get_ticker(instId="ETH-USDT-SWAP")
if ticker_resp.get("code") != "0":
    logger.error(f"行情接口失败: {ticker_resp}")
    sys.exit(2)
last_px = ticker_resp["data"][0]["last"]
logger.success(f"行情 OK: ETH-USDT-SWAP 最新价 = {last_px}")

# 2) 私有账户(验证签名) —— 查 USDT 余额
account = AccountAPI(API_KEY, SECRET_KEY, PASSPHRASE, flag=flag)
bal_resp = account.get_account_balance(ccy="USDT")
if bal_resp.get("code") != "0":
    logger.error(f"账户接口失败: {bal_resp}")
    logger.error("常见原因: key/secret/passphrase 配错;模拟盘开关与 key 来源不匹配;IP 不在白名单")
    sys.exit(3)

data = bal_resp.get("data", [])
if data and data[0].get("details"):
    details = data[0]["details"]
    for d in details:
        if d.get("ccy") == "USDT":
            logger.success(f"账户 OK: USDT 余额 = {d.get('eq')}  可用 = {d.get('availBal')}")
            break
    else:
        logger.warning("账户里暂无 USDT 余额(模拟盘默认会发放,实盘需自行充值)")
else:
    logger.warning(f"账户响应为空: {bal_resp}")

logger.success("✅ 连通性测试通过,可以进入下一步")
