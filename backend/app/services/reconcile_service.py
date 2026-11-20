from __future__ import annotations
import sqlite3

from app.config import SEALED_BY_BATCH
from app.db import transaction
from app.services.gate import batch_evidence_issues, assert_day_unlocked
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

def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    """按当前三腿现场生成一个 open 批，并固化指纹与匹配/差额明细。

    上一批 open 批标记为 superseded（作废），其明细保留供审计；
    作废批不再可用于结案。sealed / disputed 日一律拒绝重跑，
    disputed 证据修复前禁止生成第二份现行。
    """
    # 闸口在写事务外：sealed 证据不齐时先降级并独立提交。
    assert_day_unlocked(conn, dock_day)
    with transaction(conn):
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

    全局口径（证据表自洽）：
    1. 无批、伪造批 id、批日不符、批已作废/已过期、指纹不符，一律拒绝；
    2. 当日存在 sealed / disputed 批一律拒绝——证据修复前禁止第二份现行；
    3. 封印前强制批头 = 明细重算（对齐/差额行数、指纹、内容摘要），
       头明细不符直接拒绝，不允许封出缺证据的 sealed；
    4. 仍有 pending 差额拒绝；
    5. 封印与证据快照在同一 BEGIN IMMEDIATE 事务内原子完成，
       部分唯一索引保证同日至多一个 sealed/disputed 锁行。
    """
    if batch_id is None or not fingerprint:
        raise ValueError("结案必须引用未过期的 open 对账批（batch_id + fingerprint），无批不得封印")

    # 入口闸口（事务外）：含 sealed 证据不齐 -> disputed 的独立降级提交。
    assert_day_unlocked(conn, dock_day)

    with transaction(conn):
        # 事务内复检锁状态，封堵与并结案的穿插。
        lock = conn.execute(
            "SELECT id FROM reconcile_batches "
            "WHERE dock_day=? AND status IN ('sealed','disputed')",
            (dock_day,),
        ).fetchone()
        if lock is not None:
            raise ValueError(f"到货日 {dock_day} 已存在结案/争议批，不能重复封印")

        batch = _load_batch_for_seal(conn, dock_day, batch_id, fingerprint)

        # 硬核对：批头计数必须等于按明细重算，指纹必须与明细一致。
        # 只改提示文案、不核对证据的封一律视为未收口。
        issues = batch_evidence_issues(conn, batch)
        if issues:
            raise ValueError(
                "对账批 " + batch["batch_no"] + " 证据不自洽，拒绝封印：" + "、".join(issues)
            )

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
            # 唯一索引兜底：并发两路结案/争议只可能有一个锁行。
            raise ValueError(f"到货日 {dock_day} 已被并发结案，不能重复封印") from e
        if cur.rowcount != 1:
            raise ValueError(f"对账批 {batch['batch_no']} 状态已变化（可能被并发作废），拒绝封印")

        sealed = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)
        ).fetchone()
        # 封印与证据快照同事务原子提交；不再存在只刷总览的旁路任务，
        # 日封状态的唯一事实源就是本证据行。
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
        if batch["status"] in ("sealed", "disputed"):
            raise ValueError(f"已结案/争议批次（{batch['status']}）不可改差额命运")
        if batch["status"] != "open":
            raise ValueError(f"对账批 {batch['batch_no']} 已作废，不可改差额命运")
        conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))
        return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())
