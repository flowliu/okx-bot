# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""每日交易统计的本地缓存（SQLite）。

OKX get_account_bills 只保留 7 天，本表把每日聚合落地，便于长期回溯。
表结构很薄：每天一行，可被后续 sync 覆盖（同一天的数据会随交易增加）。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from orbitai import runtime as _rt

DB_FILE = _rt.db_path("stats.db")
_lock = threading.Lock()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_FILE, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date       TEXT PRIMARY KEY,   -- YYYY-MM-DD（本地时区）
                trades     INTEGER NOT NULL,
                closes     INTEGER NOT NULL,
                gross_pnl  REAL    NOT NULL,
                fee        REAL    NOT NULL,
                net_pnl    REAL    NOT NULL,
                updated_at INTEGER NOT NULL    -- unix ts (s)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sync_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def upsert_day(row: dict, now_ts: int) -> None:
    with _lock, _conn() as c:
        c.execute("""
            INSERT INTO daily_stats(date,trades,closes,gross_pnl,fee,net_pnl,updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                trades=excluded.trades,
                closes=excluded.closes,
                gross_pnl=excluded.gross_pnl,
                fee=excluded.fee,
                net_pnl=excluded.net_pnl,
                updated_at=excluded.updated_at
        """, (
            row["date"], row["trades"], row["closes"],
            row["gross_pnl"], row["fee"], row["net_pnl"],
            now_ts,
        ))


def get_range(start_date: str, end_date: str) -> list[dict]:
    """[start_date, end_date] 闭区间，按日期升序返回。"""
    with _lock, _conn() as c:
        rows = c.execute("""
            SELECT date,trades,closes,gross_pnl,fee,net_pnl,updated_at
              FROM daily_stats
             WHERE date >= ? AND date <= ?
             ORDER BY date ASC
        """, (start_date, end_date)).fetchall()
    return [dict(r) for r in rows]


def set_meta(key: str, value: str) -> None:
    with _lock, _conn() as c:
        c.execute("""
            INSERT INTO sync_meta(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))


def get_meta(key: str) -> str | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def earliest_date() -> str | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT MIN(date) AS d FROM daily_stats").fetchone()
    return r["d"] if r and r["d"] else None
