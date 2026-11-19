from __future__ import annotations
import os
import tempfile
from pathlib import Path

import pytest

# 必须在导入 app.* 之前指定隔离数据目录。
_tmp = tempfile.mkdtemp(prefix="tridock-test-")
os.environ["TRIDOCK_DATA"] = _tmp
os.environ["TRIDOCK_BATCH_TTL_SECONDS"] = "1800"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import SCHEMA, _migrate  # noqa: E402

@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "tridock.db"
    import app.config as cfg
    import app.db as dbmod

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    monkeypatch.setattr(dbmod, "DATA_DIR", tmp_path)

    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.close()

    from app.seed import seed_if_empty

    seed_if_empty()
    return TestClient(app)

def _run(client, day="2026-11-18"):
    r = client.post("/api/reconcile/run", json={"dock_day": day})
    assert r.status_code == 200, r.text
    return r.json()

def _resolve_all(client, day="2026-11-18"):
    batches = client.get(f"/api/reconcile/batches?dock_day={day}").json()
    open_batch = next(b for b in batches if b["status"] == "open")
    vs = client.get(f"/api/reconcile/variances?batch_id={open_batch['id']}").json()
    for v in vs:
        if v["fate"] == "pending":
            r = client.post(
                f"/api/reconcile/variances/{v['id']}/fate", json={"fate": "accept_short"}
            )
            assert r.status_code == 200, r.text
    return open_batch

def _seal(client, day, batch_id, fingerprint):
    return client.post(
        "/api/reconcile/seal",
        json={"dock_day": day, "batch_id": batch_id, "fingerprint": fingerprint},
    )

def test_seal_without_batch_is_rejected(client):
    r = client.post("/api/reconcile/seal", json={"dock_day": "2026-11-18"})
    assert r.status_code == 400
    assert "无批不得封印" in r.json()["detail"]

    r = client.post("/api/reconcile/seal", json={"dock_day": "2026-11-18", "batch_id": 1})
    assert r.status_code == 400

def test_seal_with_batch_snapshots_evidence(client):
    batch = _run(client)
    open_batch = _resolve_all(client)
    assert open_batch["id"] == batch["id"]

    r = _seal(client, "2026-11-18", batch["id"], batch["fingerprint"])
    assert r.status_code == 200, r.text
    sealed = r.json()
    assert sealed["status"] == "sealed"
    assert sealed["sealed_by"] == "batch"
    assert sealed["sealed_fingerprint"] == batch["fingerprint"]
    assert sealed["sealed_match_count"] == batch["matched_count"]
    assert sealed["sealed_variance_count"] == batch["variance_count"]

    detail = client.get(f"/api/reconcile/batches/{batch['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "sealed"
    assert body["evidence_consistent"] is True
    assert len(body["matches"]) == batch["matched_count"] + batch["variance_count"]
    assert len(body["variances"]) == batch["variance_count"]

    dash = client.get("/api/dashboard").json()
    assert dash["sealed"] is True
    assert dash["seal_evidence"]["batch_id"] == batch["id"]
    assert dash["seal_evidence"]["fingerprint"] == batch["fingerprint"]

def test_forged_batch_id_rejected(client):
    _run(client)
    r = _seal(client, "2026-11-18", 99999, "sha256:deadbeef")
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]

    # 只有批号文案、没有真实指纹
    batch = client.get("/api/reconcile/batches?dock_day=2026-11-18").json()[0]
    r = _seal(client, "2026-11-18", batch["id"], batch["batch_no"])
    assert r.status_code == 400
    assert "指纹" in r.json()["detail"]

def test_seal_rejects_batch_from_other_day(client):
    batch = _run(client, "2026-11-18")
    _resolve_all(client)
    r = _seal(client, "2026-11-17", batch["id"], batch["fingerprint"])
    assert r.status_code == 400
    assert "不一致" in r.json()["detail"]

def test_seal_rejects_expired_batch(client, monkeypatch):
    batch = _run(client)
    _resolve_all(client)

    conn = connect()
    conn.execute(
        "UPDATE reconcile_batches SET created_at='2020-01-01T00:00:00Z' WHERE id=?",
        (batch["id"],),
    )
    conn.commit()
    conn.close()

    r = _seal(client, "2026-11-18", batch["id"], batch["fingerprint"])
    assert r.status_code == 400
    assert "过期" in r.json()["detail"]

def test_seal_rejects_superseded_batch(client):
    first = _run(client)
    second = _run(client)
    assert first["id"] != second["id"]
    _resolve_all(client)

    r = _seal(client, "2026-11-18", first["id"], first["fingerprint"])
    assert r.status_code == 400
    assert "作废" in r.json()["detail"]

    r = _seal(client, "2026-11-18", second["id"], second["fingerprint"])
    assert r.status_code == 200, r.text

def test_seal_rejects_tampered_detail(client):
    batch = _run(client)
    _resolve_all(client)

    conn = connect()
    conn.execute(
        "UPDATE match_rows SET expect_qty=expect_qty+7 WHERE batch_id=? AND id=(SELECT MIN(id) FROM match_rows WHERE batch_id=?)",
        (batch["id"], batch["id"]),
    )
    conn.commit()
    conn.close()

    r = _seal(client, "2026-11-18", batch["id"], batch["fingerprint"])
    assert r.status_code == 400
    assert "指纹" in r.json()["detail"]

def test_seal_rejects_pending_variance(client):
    batch = _run(client)
    r = _seal(client, "2026-11-18", batch["id"], batch["fingerprint"])
    assert r.status_code == 400
    assert "差额未处置" in r.json()["detail"]

def test_write_gate_after_seal(client):
    batch = _run(client)
    _resolve_all(client)
    r = _seal(client, "2026-11-18", batch["id"], batch["fingerprint"])
    assert r.status_code == 200, r.text

    for path, payload in [
        ("/api/expects", {"doc_no": "EX-9001", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 5}),
        ("/api/receives", {"receive_no": "RC-9001", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 5}),
        ("/api/confirms", {"confirm_no": "CF-9001", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 5}),
    ]:
        resp = client.post(path, json=payload)
        assert resp.status_code == 400, (path, resp.text)
        assert "已结案" in resp.json()["detail"]

    # 结案后不能重跑对账
    r = client.post("/api/reconcile/run", json={"dock_day": "2026-11-18"})
    assert r.status_code == 400

def test_concurrent_seal_only_one_wins(client):
    import threading

    batch = _run(client)
    _resolve_all(client)

    results: list[int] = []
    lock = threading.Lock()

    def worker():
        c = TestClient(app)
        r = _seal(c, "2026-11-18", batch["id"], batch["fingerprint"])
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 400], results

    conn = connect()
    sealed = conn.execute(
        "SELECT COUNT(*) c FROM reconcile_batches WHERE dock_day='2026-11-18' AND status='sealed'"
    ).fetchone()["c"]
    assert sealed == 1
    row = conn.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day='2026-11-18' AND status='sealed'"
    ).fetchone()
    # 没有半截 sealed：快照必须齐全
    assert row["sealed_fingerprint"]
    assert row["sealed_match_count"] is not None
    assert row["sealed_variance_count"] is not None
    conn.close()

def test_batch_detail_404(client):
    r = client.get("/api/reconcile/batches/424242")
    assert r.status_code == 404
