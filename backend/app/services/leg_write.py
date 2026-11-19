from __future__ import annotations

import sqlite3

from app.services import gate

def guard_expect(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable(conn, dock_day)

def guard_receive(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable_except_receive(conn, dock_day)

def guard_confirm(conn: sqlite3.Connection, dock_day: str) -> None:
    gate.assert_writable(conn, dock_day)
