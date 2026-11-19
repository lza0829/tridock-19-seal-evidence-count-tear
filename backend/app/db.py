from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DATA_DIR, DB_PATH

def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

@contextmanager
def transaction(conn: sqlite3.Connection):
    """以 BEGIN IMMEDIATE 串行化写事务（结案 / 跑对账 / 三腿写入）。

    sqlite3 默认在第一条 DML 前才隐式 BEGIN，多个连接的“先查后写”会互相穿插。
    这里显式拿写锁，保证指纹校验、计数核对和状态迁移在同一临界区内完成。
    """
    if conn.in_transaction:
        raise RuntimeError("transaction() 不能嵌套")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
