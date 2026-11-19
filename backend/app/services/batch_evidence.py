from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.config import BATCH_TTL_SECONDS

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def expires_at(batch: sqlite3.Row | dict) -> str | None:
    created = parse_iso(batch["created_at"])
    if created is None:
        return None
    from datetime import timedelta

    return (created + timedelta(seconds=BATCH_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")

def is_expired(batch: sqlite3.Row | dict) -> bool:
    deadline = parse_iso(expires_at(batch))
    return deadline is None or deadline <= datetime.now(timezone.utc)

def _qty(value: float) -> float:
    return round(float(value), 6)

def compute_fingerprint(conn: sqlite3.Connection, batch: sqlite3.Row | dict) -> str:
    matches = conn.execute(
        """SELECT supplier_code, sku, expect_qty, receive_qty, confirm_qty, aligned
           FROM match_rows WHERE batch_id=? ORDER BY supplier_code, sku""",
        (batch["id"],),
    ).fetchall()
    variances = conn.execute(
        """SELECT supplier_code, sku, expect_qty, receive_qty, confirm_qty
           FROM variance_rows WHERE batch_id=? ORDER BY supplier_code, sku""",
        (batch["id"],),
    ).fetchall()
    payload = {
        "v": 1,
        "batch_no": batch["batch_no"],
        "dock_day": batch["dock_day"],
        "matched_count": int(batch["matched_count"]),
        "variance_count": int(batch["variance_count"]),
        "matches": [
            {
                "supplier_code": r["supplier_code"],
                "sku": r["sku"],
                "expect_qty": _qty(r["expect_qty"]),
                "receive_qty": _qty(r["receive_qty"]),
                "confirm_qty": _qty(r["confirm_qty"]),
                "aligned": int(r["aligned"]),
            }
            for r in matches
        ],
        "variances": [
            {
                "supplier_code": r["supplier_code"],
                "sku": r["sku"],
                "expect_qty": _qty(r["expect_qty"]),
                "receive_qty": _qty(r["receive_qty"]),
                "confirm_qty": _qty(r["confirm_qty"]),
            }
            for r in variances
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

def batch_view(conn: sqlite3.Connection, batch: sqlite3.Row | dict) -> dict:
    """对外的批视图：附带有效期、当前指纹是否与明细一致。"""
    data = dict(batch)
    recomputed = compute_fingerprint(conn, batch)
    data["fingerprint_valid"] = bool(batch["fingerprint"]) and recomputed == batch["fingerprint"]
    data["recomputed_fingerprint"] = recomputed
    data["expires_at"] = expires_at(batch)
    data["expired"] = is_expired(batch) and batch["status"] == "open"
    return data
