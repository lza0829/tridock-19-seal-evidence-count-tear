from __future__ import annotations
import sqlite3

from app.config import SEALED_BY_BATCH
from app.db import transaction
from app.services.overview_paint import paint_sealed
from app.services.batch_evidence import (
    batch_view,
    compute_fingerprint,
    is_expired,
    now_iso,
)

def _sum_map(conn: sqlite3.Connection, table: str, dock_day: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        f"SELECT supplier_code, sku, SUM(qty) q FROM {table} WHERE dock_day=? GROUP BY supplier_code, sku",
        (dock_day,),
    ).fetchall()
    return {(r["supplier_code"], r["sku"]): float(r["q"]) for r in rows}

def _day_state(conn: sqlite3.Connection, dock_day: str) -> None:
    sealed = conn.execute(
        "SELECT batch_no FROM reconcile_batches WHERE dock_day=? AND status='sealed'",
        (dock_day,),
    ).fetchone()
    if sealed:
        raise ValueError(f"到货日 {dock_day} 已结案（批 {sealed['batch_no']}），不能重跑对账")

def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    """按当前三腿现场生成一个 open 批，并固化指纹与匹配/差额明细。

    上一批 open 批标记为 superseded（作废），其明细保留供审计；
    作废批不再可用于结案。
    """
    with transaction(conn):
        _day_state(conn, dock_day)

        now = now_iso()
        conn.execute(
            "UPDATE reconcile_batches SET status='superseded', superseded_at=? "
            "WHERE dock_day=? AND status='open'",
            (now, dock_day),
        )

        expect_m = _sum_map(conn, "expect_lines", dock_day)
        receive_m = _sum_map(conn, "receive_lines", dock_day)
        confirm_m = _sum_map(conn, "confirm_lines", dock_day)
        keys = set(expect_m) | set(receive_m) | set(confirm_m)

        n = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
        batch_no = f"RB-{dock_day.replace('-', '')}-{n:03d}"
        cur = conn.execute(
            '''INSERT INTO reconcile_batches
               (batch_no, dock_day, matched_count, variance_count, status,
                created_at, fingerprint, sealed_at, note)
               VALUES (?,?,0,0,'open',?,'',NULL,?)''',
            (batch_no, dock_day, now, "三联对齐"),
        )
        batch_id = int(cur.lastrowid)

        matched = 0
        variance = 0
        for supplier_code, sku in sorted(keys):
            eq = expect_m.get((supplier_code, sku), 0.0)
            rq = receive_m.get((supplier_code, sku), 0.0)
            cq = confirm_m.get((supplier_code, sku), 0.0)
            aligned = 1 if eq == rq == cq else 0
            conn.execute(
                '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku,
                                         expect_qty, receive_qty, confirm_qty, aligned)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (batch_id, dock_day, supplier_code, sku, eq, rq, cq, aligned),
            )
            if aligned:
                matched += 1
            else:
                variance += 1
                conn.execute(
                    '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku,
                                                expect_qty, receive_qty, confirm_qty, fate, created_at)
                       VALUES (?,?,?,?,?,?,?,'pending',?)''',
                    (batch_id, dock_day, supplier_code, sku, eq, rq, cq, now),
                )

        conn.execute(
            "UPDATE reconcile_batches SET matched_count=?, variance_count=? WHERE id=?",
            (matched, variance, batch_id),
        )
        batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone()
        fingerprint = compute_fingerprint(conn, batch)
        conn.execute("UPDATE reconcile_batches SET fingerprint=? WHERE id=?", (fingerprint, batch_id))
        batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone()
        return batch_view(conn, batch)

def _load_batch_for_seal(
    conn: sqlite3.Connection, dock_day: str, batch_id: int, fingerprint: str
) -> sqlite3.Row:
    batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        raise ValueError(f"对账批 {batch_id} 不存在，拒绝封印")
    if batch["dock_day"] != dock_day:
        raise ValueError(
            f"对账批 {batch['batch_no']} 属于到货日 {batch['dock_day']}，与结案日 {dock_day} 不一致，拒绝封印"
        )
    if batch["status"] == "sealed":
        raise ValueError(f"到货日 {dock_day} 已由批 {batch['batch_no']} 结案，不能重复封印")
    if batch["status"] != "open":
        raise ValueError(f"对账批 {batch['batch_no']} 已作废（{batch['status']}），拒绝封印")
    if is_expired(batch):
        raise ValueError(f"对账批 {batch['batch_no']} 已过期，请重新跑三联对账后再结案")

    if not fingerprint.strip() or fingerprint.strip() == batch["batch_no"]:
        raise ValueError("结案必须携带对账批指纹，不能只传批号")
    recomputed = compute_fingerprint(conn, batch)
    if not batch["fingerprint"] or recomputed != batch["fingerprint"]:
        raise ValueError(f"对账批 {batch['batch_no']} 指纹与明细不一致（明细被改动），拒绝封印")
    if fingerprint != batch["fingerprint"]:
        raise ValueError(f"请求指纹与对账批 {batch['batch_no']} 指纹不匹配，拒绝封印")
    return batch

def seal_day(
    conn: sqlite3.Connection,
    dock_day: str,
    batch_id: int | None = None,
    fingerprint: str | None = None,
) -> dict:
    """结案封印：必须绑定未过期的 open 批 id + 指纹。

    无批、伪造批 id、批日不符、批已作废/已过期、指纹不符，一律拒绝。
    封印后批转 sealed，并快照指纹与匹配/差额计数。
    """
    if batch_id is None or not fingerprint:
        raise ValueError("结案必须引用未过期的 open 对账批（batch_id + fingerprint），无批不得封印")

    with transaction(conn):
        existing = conn.execute(
            "SELECT batch_no FROM reconcile_batches WHERE dock_day=? AND status='sealed'",
            (dock_day,),
        ).fetchone()
        if existing:
            raise ValueError(f"到货日 {dock_day} 已由批 {existing['batch_no']} 结案，不能重复封印")

        batch = _load_batch_for_seal(conn, dock_day, batch_id, fingerprint)

        # 指纹锁定后，批明细与计数必须自洽，避免结案成功却点不开证据。
        rows = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(aligned),0) a FROM match_rows WHERE batch_id=?",
            (batch["id"],),
        ).fetchone()

        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
            (batch["id"],),
        ).fetchone()["c"]
        if pending:
            raise ValueError(f"仍有 {pending} 条差额未处置，不能结案")

        now = now_iso()
        try:
            cur = conn.execute(
                '''UPDATE reconcile_batches
                   SET status='sealed', sealed_at=?, sealed_by=?,
                       sealed_fingerprint=?, sealed_match_count=?, sealed_variance_count=?
                   WHERE id=? AND status='open' ''',
                (
                    now,
                    SEALED_BY_BATCH,
                    batch["fingerprint"],
                    batch["matched_count"],
                    batch["variance_count"],
                    batch["id"],
                ),
            )
        except sqlite3.IntegrityError as e:
            # 唯一索引兜底：并发两路结案只可能有一个成功。
            raise ValueError(f"到货日 {dock_day} 已被并发结案，不能重复封印") from e
        if cur.rowcount != 1:
            raise ValueError(f"对账批 {batch['batch_no']} 状态已变化（可能被并发作废），拒绝封印")

        sealed = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)
        ).fetchone()
        paint_sealed(conn, dock_day)
        return batch_view(conn, sealed)

def set_variance_fate(conn: sqlite3.Connection, variance_id: int, fate: str) -> dict:
    allowed = {"pending", "accept_short", "accept_over", "quarantine"}
    if fate not in allowed:
        raise ValueError("非法差额命运")
    with transaction(conn):
        row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
        if not row:
            raise ValueError("差额行不存在")
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (row["batch_id"],)
        ).fetchone()
        if batch is None:
            raise ValueError("差额行所属对账批不存在")
        if batch["status"] == "sealed":
            raise ValueError("已结案批次不可改差额命运")
        if batch["status"] != "open":
            raise ValueError(f"对账批 {batch['batch_no']} 已作废，不可改差额命运")
        conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))
        return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())
