from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services import reconcile_service as svc
from app.services.batch_evidence import batch_view
from app.services.gate import batch_evidence_issues

router = APIRouter(tags=["reconcile"])

class RunIn(BaseModel):
    dock_day: str

class SealIn(BaseModel):
    dock_day: str
    batch_id: int | None = None
    fingerprint: str | None = None

class FateIn(BaseModel):
    fate: str

@router.post("/reconcile/run")
def run_reconcile(body: RunIn):
    conn = connect()
    try:
        return svc.run_reconcile(conn, body.dock_day)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()

@router.post("/reconcile/seal")
def seal(body: SealIn):
    conn = connect()
    try:
        return svc.seal_day(conn, body.dock_day, body.batch_id, body.fingerprint)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()

def _batch_payload(conn, rows) -> list[dict]:
    return [batch_view(conn, r) for r in rows]

@router.get("/reconcile/batches")
def batches(dock_day: str | None = None):
    conn = connect()
    try:
        if dock_day:
            rows = conn.execute(
                "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reconcile_batches ORDER BY id DESC").fetchall()
        return _batch_payload(conn, rows)
    finally:
        conn.close()

@router.get("/reconcile/batches/{batch_id}")
def batch_detail(batch_id: int):
    """批详情：批头（含封印快照）+ 对齐行 + 差额行。

    结案成功的批次必须能在这里点开完整证据；查不到即 404。
    """
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, f"对账批 {batch_id} 不存在")
        data = batch_view(conn, batch)
        data["matches"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM match_rows WHERE batch_id=? ORDER BY supplier_code, sku",
                (batch_id,),
            ).fetchall()
        ]
        data["variances"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY supplier_code, sku",
                (batch_id,),
            ).fetchall()
        ]
        if batch["status"] in ("sealed", "disputed"):
            # 统一证据口径：批头/明细行数/指纹/封印快照任一不符即不自洽。
            issues = batch_evidence_issues(conn, batch)
            data["evidence_issues"] = issues
            data["evidence_consistent"] = not issues
        else:
            data["evidence_consistent"] = None
            data["evidence_issues"] = []
        return data
    finally:
        conn.close()

@router.get("/reconcile/matches")
def matches(batch_id: int):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM match_rows WHERE batch_id=? ORDER BY supplier_code, sku", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@router.get("/reconcile/variances")
def variances(batch_id: int | None = None, dock_day: str | None = None):
    conn = connect()
    try:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        elif dock_day:
            rows = conn.execute(
                "SELECT * FROM variance_rows WHERE dock_day=? ORDER BY id", (dock_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@router.post("/reconcile/variances/{variance_id}/fate")
def set_fate(variance_id: int, body: FateIn):
    conn = connect()
    try:
        return svc.set_variance_fate(conn, variance_id, body.fate)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()
