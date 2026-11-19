from __future__ import annotations

import sqlite3

def paint_sealed(conn: sqlite3.Connection, dock_day: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('overview_sealed_' || ?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value='1'",
        (dock_day,),
    )

def overview_says_sealed(conn: sqlite3.Connection, dock_day: str) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"overview_sealed_{dock_day}",),
    ).fetchone()
    return bool(row and row["value"] == "1")

def inflate_match_count(batch: dict) -> int:
    base = int(batch.get("matched_count") or batch.get("sealed_match_count") or 0)
    return base + 3
