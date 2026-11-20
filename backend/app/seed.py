from __future__ import annotations
import sqlite3
from app.db import connect

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expect_lines (
  id INTEGER PRIMARY KEY,
  doc_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receive_lines (
  id INTEGER PRIMARY KEY,
  receive_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  occurred_at TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirm_lines (
  id INTEGER PRIMARY KEY,
  confirm_no TEXT NOT NULL UNIQUE,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  dock_day TEXT NOT NULL,
  qty REAL NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconcile_batches (
  id INTEGER PRIMARY KEY,
  batch_no TEXT NOT NULL UNIQUE,
  dock_day TEXT NOT NULL,
  matched_count INTEGER NOT NULL,
  variance_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT,
  fingerprint TEXT NOT NULL DEFAULT '',
  sealed_at TEXT,
  sealed_by TEXT,
  sealed_fingerprint TEXT,
  sealed_match_count INTEGER,
  sealed_variance_count INTEGER,
  superseded_at TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS match_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  expect_qty REAL NOT NULL,
  receive_qty REAL NOT NULL,
  confirm_qty REAL NOT NULL,
  aligned INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS variance_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  dock_day TEXT NOT NULL,
  supplier_code TEXT NOT NULL,
  sku TEXT NOT NULL,
  expect_qty REAL NOT NULL,
  receive_qty REAL NOT NULL,
  confirm_qty REAL NOT NULL,
  fate TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS late_spill (
  id INTEGER PRIMARY KEY,
  dock_day TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS reopen_requests (
  id INTEGER PRIMARY KEY,
  dock_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none'
);
'''

# 旧库升级：reconcile_batches 缺的列在此补齐。
_BATCH_COLUMNS = {
    "created_at": "TEXT",
    "fingerprint": "TEXT NOT NULL DEFAULT ''",
    "sealed_by": "TEXT",
    "sealed_fingerprint": "TEXT",
    "sealed_match_count": "INTEGER",
    "sealed_variance_count": "INTEGER",
    "superseded_at": "TEXT",
}

def _migrate(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(reconcile_batches)")}
    for name, decl in _BATCH_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE reconcile_batches ADD COLUMN {name} {decl}")
    # 每一天最多一个结案/争议锁行（sealed 或 disputed）：并发结案、
    # 缺证据降级与第二份现行的最后一道硬约束。
    conn.execute("DROP INDEX IF EXISTS ux_reconcile_batches_sealed")
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_reconcile_batches_day_lock "
            "ON reconcile_batches(dock_day) WHERE status IN ('sealed','disputed')"
        )
    except sqlite3.IntegrityError:
        # 历史脏数据里已有同日多锁行时，保留现状；应用层仍会拒绝双封。
        pass
    # 旧版“只刷总览”的假绿标志全部清除：日封状态只以证据表为准。
    conn.execute("DELETE FROM settings WHERE key LIKE 'overview_sealed_%'")
    conn.commit()

def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if conn.execute("SELECT COUNT(*) c FROM suppliers").fetchone()["c"]:
            return
        conn.execute("INSERT INTO settings(key, value) VALUES ('dock_day', ?)", ("2026-11-18",))
        suppliers = [
            ("SUP-A", "东海食材"),
            ("SUP-B", "北岭包装"),
            ("SUP-C", "南湾百货"),
        ]
        for code, title in suppliers:
            conn.execute("INSERT INTO suppliers(code, title) VALUES (?,?)", (code, title))
        expects = [
            ("EX-1001", "SUP-A", "SKU-米-25", "2026-11-18", 40, "早班预报"),
            ("EX-1002", "SUP-A", "SKU-油-5", "2026-11-18", 20, None),
            ("EX-1003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, None),
            ("EX-1004", "SUP-C", "SKU-巾-卷", "2026-11-18", 60, None),
            ("EX-0901", "SUP-A", "SKU-米-25", "2026-11-17", 10, "昨日"),
        ]
        for row in expects:
            conn.execute(
                '''INSERT INTO expect_lines(doc_no, supplier_code, sku, dock_day, qty, note, created_at)
                   VALUES (?,?,?,?,?,?,?)''',
                (*row, "2026-11-18T08:00:00Z"),
            )
        receives = [
            ("RC-2001", "SUP-A", "SKU-米-25", "2026-11-18", 38, "2026-11-18T09:10:00Z", "少2"),
            ("RC-2002", "SUP-A", "SKU-油-5", "2026-11-18", 20, "2026-11-18T09:20:00Z", None),
            ("RC-2003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, "2026-11-18T10:00:00Z", None),
            ("RC-2004", "SUP-C", "SKU-巾-卷", "2026-11-18", 66, "2026-11-18T11:00:00Z", "多6"),
        ]
        for row in receives:
            conn.execute(
                '''INSERT INTO receive_lines(receive_no, supplier_code, sku, dock_day, qty, occurred_at, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (*row, row[5]),
            )
        confirms = [
            ("CF-3001", "SUP-A", "SKU-米-25", "2026-11-18", 38, "回执按实收"),
            ("CF-3002", "SUP-A", "SKU-油-5", "2026-11-18", 20, None),
            ("CF-3003", "SUP-B", "SKU-箱-大", "2026-11-18", 100, None),
            ("CF-3004", "SUP-C", "SKU-巾-卷", "2026-11-18", 60, "回执仍报预报数"),
        ]
        for row in confirms:
            conn.execute(
                '''INSERT INTO confirm_lines(confirm_no, supplier_code, sku, dock_day, qty, note, created_at)
                   VALUES (?,?,?,?,?,?,?)''',
                (*row, "2026-11-18T12:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()
