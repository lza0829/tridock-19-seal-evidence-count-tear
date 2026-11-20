from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services.batch_evidence import batch_view
from app.services.gate import batch_evidence_issues, read_day_lock

router = APIRouter(tags=["dashboard"])

@router.get("/dashboard")
def dashboard():
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='dock_day'").fetchone()
        day = row["value"] if row else None
        expect_n = receive_n = confirm_n = 0
        batch = None
        pending = 0
        disputed = False
        lock_issues: list[str] = []
        sealed = False
        if day:
            expect_n = conn.execute(
                "SELECT COUNT(*) c FROM expect_lines WHERE dock_day=?", (day,)
            ).fetchone()["c"]
            receive_n = conn.execute(
                "SELECT COUNT(*) c FROM receive_lines WHERE dock_day=?", (day,)
            ).fetchone()["c"]
            confirm_n = conn.execute(
                "SELECT COUNT(*) c FROM confirm_lines WHERE dock_day=?", (day,)
            ).fetchone()["c"]
            # 日封状态的唯一事实源：证据表中的 sealed/disputed 锁行。
            # 不再存在 settings 刷绿标志，总览不可能脱离证据单独显已封。
            lock = read_day_lock(conn, day)
            sealed = lock is not None and lock["status"] == "sealed"
            disputed = lock is not None and lock["status"] == "disputed"
            if lock is not None:
                lock_issues = batch_evidence_issues(conn, lock)
                # 读路径不降级、不落库，只如实暴露自洽性，写/结案入口才执行降级。
                if lock["status"] == "sealed" and lock_issues:
                    disputed = True
                    sealed = False
            batch = lock or conn.execute(
                "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='open' "
                "ORDER BY id DESC LIMIT 1",
                (day,),
            ).fetchone()
            if batch is not None and not sealed and not disputed:
                pending = conn.execute(
                    "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
                    (batch["id"],),
                ).fetchone()["c"]

        latest = batch_view(conn, batch) if batch is not None else None
        evidence = None
        if (sealed or disputed) and latest is not None:
            # 摘要计数直接取封印快照（无快照则取批头），绝不虚增，
            # 与证据页按明细重算的结果必须一致。
            evidence = {
                "batch_id": latest["id"],
                "batch_no": latest["batch_no"],
                "fingerprint": latest.get("sealed_fingerprint") or latest.get("fingerprint"),
                "matched_count": (
                    latest["sealed_match_count"]
                    if latest.get("sealed_match_count") is not None
                    else latest.get("matched_count")
                ),
                "variance_count": (
                    latest["sealed_variance_count"]
                    if latest.get("sealed_variance_count") is not None
                    else latest.get("variance_count")
                ),
                "sealed_at": latest.get("sealed_at"),
                "sealed_by": latest.get("sealed_by"),
                "issues": lock_issues,
            }
        return {
            "dock_day": day,
            "sealed": sealed,
            "disputed": disputed,
            "lock_issues": lock_issues,
            "seal_evidence": evidence,
            "expect_count": expect_n,
            "receive_count": receive_n,
            "confirm_count": confirm_n,
            "latest_batch": latest,
            "pending_variance": pending,
            "can_seal": (not sealed) and (not disputed) and latest is not None
            and latest.get("status") == "open"
            and not latest.get("expired"),
        }
    finally:
        conn.close()
