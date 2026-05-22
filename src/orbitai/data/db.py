# Copyright (c) 2026 D.L <103242127@qq.com>
# Licensed under the MIT License. See LICENSE file in the project root.
"""SQLite 持久化

三张表:
  grids:  每个网格格位的当前状态(挂单 ID、状态机)
  trades: 成交流水,用于事后复盘
  meta:   单例配置(中心价、启动时间等)
"""
import sqlite3
import time
from contextlib import contextmanager
from orbitai.config.defaults import DB_PATH as _DB_NAME
from orbitai import runtime as _rt

DB_PATH = _rt.db_path(_DB_NAME)

SCHEMA = """
CREATE TABLE IF NOT EXISTS grids (
    level         INTEGER PRIMARY KEY,    -- 格位序号: 负数=下方(多头格), 正数=上方(空头格)
    price         REAL    NOT NULL,       -- 该格的挂单价
    direction     TEXT    NOT NULL,       -- 'long' (下方,开多平多) | 'short' (上方,开空平空)
    phase         TEXT    NOT NULL,       -- 'open' (挂开仓单) | 'close' (挂平仓单)
    sz            REAL    NOT NULL,       -- 张数
    ord_id        TEXT,                   -- 当前挂着的订单 ID; 没单时为 NULL
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    level         INTEGER NOT NULL,
    phase         TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    pos_side      TEXT    NOT NULL,
    price         REAL    NOT NULL,
    sz            REAL    NOT NULL,
    ord_id        TEXT    NOT NULL,
    fee           REAL
);

CREATE TABLE IF NOT EXISTS meta (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);

-- AI 完全驱动模式下,每对 (open + close) 是一个 slot
CREATE TABLE IF NOT EXISTS ai_slots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    side          TEXT    NOT NULL,        -- 'long' / 'short'
    phase         TEXT    NOT NULL,        -- 'open' (open 挂着) / 'close' (open 已成交,close 挂着) / 'done' / 'cancelled'
    open_price    REAL    NOT NULL,
    close_price   REAL    NOT NULL,
    sz            REAL    NOT NULL,
    open_ord_id   TEXT,
    close_ord_id  TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)


def get_meta(key: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str):
    with conn() as c:
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def upsert_grid(level: int, price: float, direction: str, phase: str, sz: float, ord_id: str | None):
    with conn() as c:
        c.execute(
            "INSERT INTO grids(level,price,direction,phase,sz,ord_id,updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(level) DO UPDATE SET "
            "  price=excluded.price, direction=excluded.direction, phase=excluded.phase, "
            "  sz=excluded.sz, ord_id=excluded.ord_id, updated_at=excluded.updated_at",
            (level, price, direction, phase, sz, ord_id, int(time.time())),
        )


def update_grid_order(level: int, phase: str, ord_id: str | None, price: float | None = None):
    with conn() as c:
        if price is not None:
            c.execute(
                "UPDATE grids SET phase=?, ord_id=?, price=?, updated_at=? WHERE level=?",
                (phase, ord_id, price, int(time.time()), level),
            )
        else:
            c.execute(
                "UPDATE grids SET phase=?, ord_id=?, updated_at=? WHERE level=?",
                (phase, ord_id, int(time.time()), level),
            )


def all_grids() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM grids ORDER BY level").fetchall()


def grids_with_orders() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM grids WHERE ord_id IS NOT NULL ORDER BY level"
        ).fetchall()


def vacant_open_grids() -> list[sqlite3.Row]:
    """phase=open 且 ord_id 为空 —— 等待重挂的开仓格位
    （AI 否决/挂单失败留下的待重试坑位）"""
    with conn() as c:
        return c.execute(
            "SELECT * FROM grids WHERE phase='open' AND ord_id IS NULL ORDER BY level"
        ).fetchall()


def log_trade(level: int, phase: str, side: str, pos_side: str,
              price: float, sz: float, ord_id: str, fee: float | None = None):
    with conn() as c:
        c.execute(
            "INSERT INTO trades(ts,level,phase,side,pos_side,price,sz,ord_id,fee) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (int(time.time()), level, phase, side, pos_side, price, sz, ord_id, fee),
        )


def clear_all():
    """重置:删除所有 grids 和 meta,trades 保留作为历史。"""
    with conn() as c:
        c.execute("DELETE FROM grids")
        c.execute("DELETE FROM ai_slots")
        c.execute("DELETE FROM meta")


# ============================================================
# AI 完全驱动模式:ai_slots 操作
# ============================================================
def ai_slot_add(side: str, open_price: float, close_price: float, sz: float,
                open_ord_id: str | None) -> int:
    """新建一个 slot,phase='open'。返回 slot id。"""
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO ai_slots(side,phase,open_price,close_price,sz,open_ord_id,"
            "close_ord_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (side, "open", open_price, close_price, sz, open_ord_id, None, now, now),
        )
        return cur.lastrowid


def ai_slot_update(slot_id: int, **kw):
    """部分更新。允许字段:phase, open_ord_id, close_ord_id。"""
    allowed = {"phase", "open_ord_id", "close_ord_id"}
    fields = {k: v for k, v in kw.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    args = list(fields.values()) + [int(time.time()), slot_id]
    with conn() as c:
        c.execute(f"UPDATE ai_slots SET {sets} WHERE id=?", args)


def ai_slots_active() -> list[sqlite3.Row]:
    """所有 phase=open 或 close 的活跃 slot。"""
    with conn() as c:
        return c.execute(
            "SELECT * FROM ai_slots WHERE phase IN ('open','close') ORDER BY id"
        ).fetchall()


def ai_slots_by_phase(phase: str) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM ai_slots WHERE phase=? ORDER BY id", (phase,)
        ).fetchall()
