from __future__ import annotations

import sqlite3

from app.services import gate

# 三条旁路共用同一个写闸，不允许任何旁路自带豁免。
# guard_* 在写事务开启前调用（可触发 sealed -> disputed 降级）；
# recheck_* 在写事务内只读复检，封堵并发封印窗口。

def guard_expect(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable(conn, dock_day)

def guard_receive(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable(conn, dock_day)

def guard_confirm(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable(conn, dock_day)

def recheck_expect(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable_in_tx(conn, dock_day)

def recheck_receive(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable_in_tx(conn, dock_day)

def recheck_confirm(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable_in_tx(conn, dock_day)
