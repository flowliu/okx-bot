# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""OKX 客户端工厂

集中加载 .env 并构造各类 API 客户端,其他脚本统一从这里取实例,
避免每个文件都重复读取环境变量和写鉴权参数。
"""
import os
import sys
from dotenv import load_dotenv
from loguru import logger
from okx.Account import AccountAPI
from okx.Trade import TradeAPI
from okx.MarketData import MarketAPI
from okx.PublicData import PublicAPI

import config_loader

load_dotenv()

API_KEY = os.getenv("OKX_API_KEY", "").strip()
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()
SIMULATED = os.getenv("OKX_SIMULATED", "1").strip()  # "1" 模拟盘, "0" 实盘
FLAG = SIMULATED  # python-okx 的 flag 与 OKX_SIMULATED 取值一致

if not all([API_KEY, SECRET_KEY, PASSPHRASE]):
    logger.error("缺少 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE,请先填 .env")
    sys.exit(1)


def _domain() -> str:
    return config_loader.get("OKX_DOMAIN", "https://www.okx.com")


def market_api() -> MarketAPI:
    return MarketAPI(flag=FLAG, domain=_domain())


def public_api() -> PublicAPI:
    return PublicAPI(flag=FLAG, domain=_domain())


def account_api() -> AccountAPI:
    return AccountAPI(API_KEY, SECRET_KEY, PASSPHRASE, flag=FLAG, domain=_domain())


def trade_api() -> TradeAPI:
    return TradeAPI(API_KEY, SECRET_KEY, PASSPHRASE, flag=FLAG, domain=_domain())


def is_simulated() -> bool:
    return SIMULATED == "1"
