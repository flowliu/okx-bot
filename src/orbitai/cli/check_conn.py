# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""OKX 连通性测试

跑这个脚本验证三件事:
  1) .env 里的 key/secret/passphrase 配对正确(签名能通过)
  2) 模拟盘开关 OKX_SIMULATED 设置正确
  3) 行情接口和账户接口都能调通

用法:
    orbitai-check
"""
import os
import sys
from dotenv import load_dotenv
from loguru import logger
from okx.Account import AccountAPI
from okx.MarketData import MarketAPI


def main() -> int:
    load_dotenv()

    api_key = os.getenv("OKX_API_KEY", "").strip()
    secret_key = os.getenv("OKX_SECRET_KEY", "").strip()
    passphrase = os.getenv("OKX_PASSPHRASE", "").strip()
    simulated = os.getenv("OKX_SIMULATED", "1").strip()

    if not all([api_key, secret_key, passphrase]):
        logger.error("缺少 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE,请先填 .env")
        return 1

    mode = "模拟盘" if simulated == "1" else "实盘"
    logger.info(f"当前模式: {mode}  (OKX_SIMULATED={simulated})")

    flag = simulated

    market = MarketAPI(flag=flag)
    ticker_resp = market.get_ticker(instId="ETH-USDT-SWAP")
    if ticker_resp.get("code") != "0":
        logger.error(f"行情接口失败: {ticker_resp}")
        return 2
    last_px = ticker_resp["data"][0]["last"]
    logger.success(f"行情 OK: ETH-USDT-SWAP 最新价 = {last_px}")

    account = AccountAPI(api_key, secret_key, passphrase, flag=flag)
    bal_resp = account.get_account_balance(ccy="USDT")
    if bal_resp.get("code") != "0":
        logger.error(f"账户接口失败: {bal_resp}")
        logger.error("常见原因: key/secret/passphrase 配错;模拟盘开关与 key 来源不匹配;IP 不在白名单")
        return 3

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
