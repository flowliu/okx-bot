# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""按天统计交易盈亏 / 手续费 / 净收益

数据源：OKX 账户账单（get_account_bills，type=2 交易类）。
每条账单含 pnl（实现盈亏，仅平仓时非零）+ fee（手续费，OKX 返回负数=支出）。

用法：
    .venv/bin/python stats.py              # 今天
    .venv/bin/python stats.py yesterday    # 昨天
    .venv/bin/python stats.py week         # 最近 7 天
    .venv/bin/python stats.py 2026-05-20   # 指定单日（限近 7 天）

注意：get_account_bills 只保留最近 7 天；更早历史用 get_account_bills_archive，本脚本未集成。
"""
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

from orbitai.data.client import account_api
from orbitai.config import defaults as config


def fetch_bills_in_range(begin_ms: int, end_ms: int, instType: str = "SWAP") -> list[dict]:
    """拉指定时间段（毫秒）所有 type=2 交易账单。

    SDK 的 get_account_bills 不支持 begin/end 参数，所以拉最近账单后客户端过滤。
    OKX 默认按 ts 倒序返回；翻页用 after=billId 拉更早的。
    """
    api = account_api()
    bills: list[dict] = []
    after = ""
    pages = 0
    while True:
        # 单页 3 次重试，应对偶发 TLS 超时
        resp = None
        for attempt in range(3):
            try:
                resp = api.get_account_bills(
                    instType=instType,
                    type="2",          # 2 = 交易
                    after=after,
                    limit="100",
                )
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[ERR] get_account_bills 3 次重试均失败: {e}")
                    return bills
                wait = 0.8 * (attempt + 1)
                print(f"[WARN] get_account_bills 第 {attempt+1} 次失败({e})，{wait}s 后重试")
                time.sleep(wait)
        if not resp or resp.get("code") != "0":
            print(f"[ERR] get_account_bills 响应非 0: {resp}")
            break
        data = resp.get("data", []) or []
        if not data:
            break
        # 客户端时间过滤（数据按 ts DESC）
        stop = False
        for b in data:
            try:
                ts = int(b.get("ts", 0))
            except (TypeError, ValueError):
                continue
            if ts < begin_ms:
                stop = True
                break  # 已经早于窗口，后面更早，直接停
            if ts < end_ms:
                bills.append(b)
            # ts >= end_ms 的跳过(未来时间不应出现，但稳健起见)
        if stop:
            break
        pages += 1
        if len(data) < 100:
            break
        after = data[-1].get("billId", "")
        if not after:
            break
        time.sleep(0.1)
        if pages > 70:
            print(f"[WARN] 翻页超 70 页强制停止，已拉 {len(bills)} 条")
            break
    return bills


def summarize_by_day(bills: list[dict]) -> dict[str, dict]:
    """按日期分组聚合。返回 {date_str: {trades, gross_pnl, fee, net_pnl}}."""
    by_day: dict[str, dict] = defaultdict(lambda: {
        "trades": 0,
        "closes": 0,
        "gross_pnl": 0.0,
        "fee": 0.0,
    })
    for b in bills:
        try:
            ts = int(b.get("ts", 0))
            pnl = float(b.get("pnl") or 0)
            fee = float(b.get("fee") or 0)
        except (TypeError, ValueError):
            continue
        if pnl == 0 and fee == 0:
            continue
        d = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        bucket = by_day[d]
        bucket["trades"] += 1
        if pnl != 0:
            bucket["closes"] += 1
        bucket["gross_pnl"] += pnl
        bucket["fee"] += fee  # OKX fee 为负数

    # 计算净收益 = 盈亏 + 手续费(负数)
    for d, b in by_day.items():
        b["net_pnl"] = b["gross_pnl"] + b["fee"]
    return dict(by_day)


def parse_arg(arg: str) -> tuple[datetime, datetime, list[str]]:
    """解析参数为 [begin_ts, end_ts, 想打印的日期列表]"""
    today = datetime.now().date()
    if arg in ("", "today"):
        days = [today]
    elif arg == "yesterday":
        days = [today - timedelta(days=1)]
    elif arg == "week":
        days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    else:
        try:
            d = datetime.strptime(arg, "%Y-%m-%d").date()
            days = [d]
        except ValueError:
            raise SystemExit(f"参数 {arg!r} 不支持，用 today / yesterday / week / YYYY-MM-DD")
    start = datetime.combine(days[0], datetime.min.time())
    end = datetime.combine(days[-1], datetime.min.time()) + timedelta(days=1)
    return start, end, [d.strftime("%Y-%m-%d") for d in days]


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    arg = argv[1] if len(argv) > 1 else "today"
    start, end, day_keys = parse_arg(arg)
    begin_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    print(f"统计窗口：{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}（{config.INST_ID}）")
    bills = fetch_bills_in_range(begin_ms, end_ms)
    print(f"拉到 {len(bills)} 条 type=2 交易账单")

    by_day = summarize_by_day(bills)

    g_trades = g_closes = 0
    g_pnl = g_fee = 0.0
    for d in day_keys:
        b = by_day.get(d, {"trades": 0, "closes": 0, "gross_pnl": 0.0, "fee": 0.0, "net_pnl": 0.0})
        g_trades += b["trades"]
        g_closes += b["closes"]
        g_pnl += b["gross_pnl"]
        g_fee += b["fee"]
        print()
        print(f"=== {d} ===")
        print(f"  笔数:    {b['trades']}")
        print(f"  平仓:    {b['closes']}")
        print(f"  盈亏:    {b['gross_pnl']:+.4f} USDT")
        print(f"  手续费:  {b['fee']:+.4f} USDT")
        print(f"  净收益:  {b['net_pnl']:+.4f} USDT")

    if len(day_keys) > 1:
        print()
        print(f"=== 合计 ({day_keys[0]} ~ {day_keys[-1]}) ===")
        print(f"  笔数:    {g_trades}")
        print(f"  平仓:    {g_closes}")
        print(f"  盈亏:    {g_pnl:+.4f} USDT")
        print(f"  手续费:  {g_fee:+.4f} USDT")
        print(f"  净收益:  {(g_pnl + g_fee):+.4f} USDT")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
