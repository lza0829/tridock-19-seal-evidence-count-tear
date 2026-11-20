from __future__ import annotations
import sqlite3

from app.db import transaction
from app.services.batch_evidence import compute_fingerprint


class DayLockedError(Exception):
    """到货日已封（sealed / disputed），三旁路写入一律 409。

    全局只有这一个写闸异常：预报、实收、回执三条旁路同拒同放，
    不允许任何旁路自带豁免。
    """

    def __init__(self, dock_day: str, status: str, reasons: list[str] | None = None):
        self.dock_day = dock_day
        self.status = status
        self.reasons = reasons or []
        detail = f"到货日 {dock_day} 已结案（{status}），禁止再改预报/实收/回执"
        if reasons:
            detail += "；证据异常：" + "、".join(reasons)
        super().__init__(detail)


# ---------------------------------------------------------------------------
# 证据自洽：批头 / 明细行 / 封印快照 / 指纹必须互相吻合。
# 这是全系统唯一的证据口径，结案入口、写闸、总览、证据页都调它。
# ---------------------------------------------------------------------------

def batch_evidence_issues(
    conn: sqlite3.Connection, batch: sqlite3.Row
) -> list[str]:
    """返回证据问题清单；空列表代表证据自洽。

    核对项：
    1. 批头 matched_count / variance_count 等于按明细重算的行数；
    2. match_rows 对齐行数 == 批头 matched_count，差额行数 == 批头 variance_count；
    3. variance_rows 实际行数 == 批头 variance_count；
    4. 存储指纹非空且等于按明细重算的指纹；
    5. sealed 批另需封印快照齐全且与现行字段一致。
    """
    issues: list[str] = []
    batch_id = batch["id"]

    row_stat = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(aligned),0) a FROM match_rows WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    detail_total = int(row_stat["c"])
    detail_aligned = int(row_stat["a"])
    detail_variance_rows = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]

    header_matched = int(batch["matched_count"] or 0)
    header_variance = int(batch["variance_count"] or 0)

    if detail_total != header_matched + header_variance:
        issues.append(
            f"批头计数合计 {header_matched + header_variance} 与明细行数 {detail_total} 不一致"
        )
    if detail_aligned != header_matched:
        issues.append(
            f"批头对齐数 {header_matched} 与明细对齐行数 {detail_aligned} 不一致"
        )
    if detail_total - detail_aligned != header_variance:
        issues.append(
            f"批头差额数 {header_variance} 与明细差额行数 {detail_total - detail_aligned} 不一致"
        )
    if detail_variance_rows != header_variance:
        issues.append(
            f"批头差额数 {header_variance} 与差额表行数 {detail_variance_rows} 不一致"
        )

    if not batch["fingerprint"]:
        issues.append("批指纹缺失")
    else:
        recomputed = compute_fingerprint(conn, batch)
        if recomputed != batch["fingerprint"]:
            issues.append("批指纹与明细重算结果不一致（明细被改动）")

    if batch["status"] == "sealed":
        if not batch["sealed_fingerprint"]:
            issues.append("封印快照指纹缺失")
        elif batch["sealed_fingerprint"] != batch["fingerprint"]:
            issues.append("封印快照指纹与现行指纹不一致")
        if batch["sealed_match_count"] is None or batch["sealed_match_count"] != header_matched:
            issues.append("封印对齐计数缺失或与批头不一致")
        if batch["sealed_variance_count"] is None or batch["sealed_variance_count"] != header_variance:
            issues.append("封印差额计数缺失或与批头不一致")
        if not batch["sealed_by"] or not batch["sealed_at"]:
            issues.append("封印来源/时间缺失")
    return issues


def read_day_lock(conn: sqlite3.Connection, dock_day: str) -> sqlite3.Row | None:
    """只读查询当日锁行（sealed 优先，其次 disputed）。不落库、不降级。"""
    return conn.execute(
        "SELECT * FROM reconcile_batches "
        "WHERE dock_day=? AND status IN ('sealed','disputed') "
        "ORDER BY CASE status WHEN 'sealed' THEN 0 ELSE 1 END, id DESC LIMIT 1",
        (dock_day,),
    ).fetchone()


def enforce_day_lock(conn: sqlite3.Connection, dock_day: str) -> sqlite3.Row | None:
    """写路径/结案入口的唯一收口，自带独立短事务。

    - sealed 且证据自洽：原样返回锁行；
    - sealed 但证据不齐：降级为 disputed 并提交，再返回锁行；
    - 已是 disputed：原样返回；
    - 无锁：返回 None。
    降级必须在调用方写事务之外提交，避免被外层回滚吞掉。
    """
    if conn.in_transaction:
        raise RuntimeError("enforce_day_lock 必须在调用方事务之外调用")
    with transaction(conn):
        lock = read_day_lock(conn, dock_day)
        if lock is None or lock["status"] != "sealed":
            return lock
        issues = batch_evidence_issues(conn, lock)
        if not issues:
            return lock
        conn.execute(
            "UPDATE reconcile_batches SET status='disputed', note=? WHERE id=? AND status='sealed'",
            ("证据不齐自动降级：" + "、".join(issues), lock["id"]),
        )
        return conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (lock["id"],)
        ).fetchone()


def assert_writable(conn: sqlite3.Connection, dock_day: str) -> None:
    """三旁路写事务开启前的强制闸口（409 口径）。"""
    lock = enforce_day_lock(conn, dock_day)
    if lock is not None:
        reasons = (
            batch_evidence_issues(conn, lock) if lock["status"] == "disputed" else []
        )
        raise DayLockedError(dock_day, lock["status"], reasons)


def assert_writable_in_tx(conn: sqlite3.Connection, dock_day: str) -> None:
    """写事务内的只读复检，封堵闸口与 INSERT 之间的并发封印。"""
    lock = read_day_lock(conn, dock_day)
    if lock is not None:
        raise DayLockedError(dock_day, lock["status"])


def assert_day_unlocked(conn: sqlite3.Connection, dock_day: str) -> None:
    """结案/重跑入口口径：存在 sealed 或 disputed 一律拒绝（400）。

    证据修好并恢复为 sealed（或经处置移除锁行）之前，禁止第二份现行。
    """
    lock = enforce_day_lock(conn, dock_day)
    if lock is None:
        return
    if lock["status"] == "disputed":
        raise ValueError(
            f"到货日 {dock_day} 存在证据争议批 {lock['batch_no']}（disputed），"
            "须先修复证据，禁止再封或重跑对账"
        )
    raise ValueError(
        f"到货日 {dock_day} 已由批 {lock['batch_no']} 结案，不能重复封印或重跑对账"
    )
