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

    # 三条旁路同闸同拒：封后一律 409，不允许任何旁路（含实收）豁免。
    for path, payload in [
        ("/api/expects", {"doc_no": "EX-9001", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 5}),
        ("/api/receives", {"receive_no": "RC-9001", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 5}),
        ("/api/confirms", {"confirm_no": "CF-9001", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 5}),
    ]:
        resp = client.post(path, json=payload)
        assert resp.status_code == 409, (path, resp.text)
        assert "已结案" in resp.json()["detail"]

    # 结案后不能重跑对账
    r = client.post("/api/reconcile/run", json={"dock_day": "2026-11-18"})
    assert r.status_code == 400


def test_three_legs_share_one_gate_no_bypass(client):
    """夹具：旁路一通一拒即未收口——封后三旁路必须全部 409。

    旧实现实收旁路走空函数可写成功（假绿下仍改得到正式数量），
    这里把三旁路都打一遍，任何一条 2xx 都视为漏写。
    """
    batch = _run(client)
    _resolve_all(client)
    assert _seal(client, "2026-11-18", batch["id"], batch["fingerprint"]).status_code == 200

    bodies = [
        ("/api/expects", {"doc_no": "EX-G1", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 7}),
        ("/api/receives", {"receive_no": "RC-G1", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 7}),
        ("/api/confirms", {"confirm_no": "CF-G1", "supplier_code": "SUP-A", "sku": "SKU-米-25", "dock_day": "2026-11-18", "qty": 7}),
    ]
    for path, payload in bodies:
        resp = client.post(path, json=payload)
        assert resp.status_code == 409, (path, resp.status_code, resp.text)

    # 正式数量一条都没被改：当日三腿行数保持种子值（17 日预报不计入当日）。
    dash = client.get("/api/dashboard").json()
    assert (dash["expect_count"], dash["receive_count"], dash["confirm_count"]) == (4, 4, 4)


def test_seal_rejects_header_detail_count_tear(client):
    """夹具：头明细不符（明细有行、批头仍是零/歪计数）不得封出 sealed。"""
    from app.services.batch_evidence import compute_fingerprint

    batch = _run(client)
    _resolve_all(client)

    conn = connect()
    # 删一条明细使明细行数少于批头计数，再把指纹重算回写，
    # 确保挡住封印的是“头=明细”硬核对而非仅指纹校验。
    conn.execute(
        "DELETE FROM match_rows WHERE id=(SELECT MAX(id) FROM match_rows WHERE batch_id=?)",
        (batch["id"],),
    )
    row = conn.execute(
        "SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)
    ).fetchone()
    conn.execute(
        "UPDATE reconcile_batches SET fingerprint=? WHERE id=?",
        (compute_fingerprint(conn, row), batch["id"]),
    )
    conn.commit()
    conn.close()

    fresh = next(
        b for b in client.get("/api/reconcile/batches?dock_day=2026-11-18").json()
        if b["id"] == batch["id"]
    )
    r = _seal(client, "2026-11-18", batch["id"], fresh["fingerprint"])
    assert r.status_code == 400
    assert "证据不自洽" in r.json()["detail"]
    assert "明细行数" in r.json()["detail"]

    conn = connect()
    status = conn.execute(
        "SELECT status FROM reconcile_batches WHERE id=?", (batch["id"],)
    ).fetchone()["status"]
    conn.close()
    assert status == "open"


def test_overview_sealed_without_evidence_is_impossible(client):
    """夹具：总览已封证据空——日封只认证据表，settings 刷绿标志无效并被清理。"""
    conn = connect()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('overview_sealed_2026-11-18','1')"
    )
    conn.commit()
    conn.close()

    dash = client.get("/api/dashboard").json()
    assert dash["sealed"] is False
    assert dash["disputed"] is False
    assert dash["seal_evidence"] is None

    # 旧刷绿标志即使残留在 settings 中也完全失效：日封只以证据表为准。
    # （启动迁移会清理历史键；运行期新插入的键同样不产生任何效力。）
    conn = connect()
    leftover = conn.execute(
        "SELECT COUNT(*) c FROM settings WHERE key LIKE 'overview_sealed_%'"
    ).fetchone()["c"]
    conn.close()
    assert leftover == 1  # 残留键存在，但下面证明它不再驱动任何状态

    resp = client.post(
        "/api/receives",
        json={"receive_no": "RC-OK1", "supplier_code": "SUP-A", "sku": "SKU-油-5",
              "dock_day": "2026-11-18", "qty": 3},
    )
    assert resp.status_code == 200, resp.text


def test_tampered_sealed_batch_downgrades_disputed_and_blocks_writes(client):
    """夹具：封后证据被破坏 -> 写旁路触发 sealed 降 disputed，三旁路全拒，
    且 disputed 修复前禁止第二份现行（重跑/再封都拒绝）。"""
    batch = _run(client)
    _resolve_all(client)
    assert _seal(client, "2026-11-18", batch["id"], batch["fingerprint"]).status_code == 200

    conn = connect()
    conn.execute(
        "UPDATE match_rows SET expect_qty=expect_qty+7 WHERE batch_id=?",
        (batch["id"],),
    )
    conn.commit()
    conn.close()

    # 任意旁路写入都会触发降级并被 409 拒绝，三旁路结果一致。
    for path, payload in [
        ("/api/expects", {"doc_no": "EX-D1", "supplier_code": "SUP-A", "sku": "X", "dock_day": "2026-11-18", "qty": 1}),
        ("/api/receives", {"receive_no": "RC-D1", "supplier_code": "SUP-A", "sku": "X", "dock_day": "2026-11-18", "qty": 1}),
        ("/api/confirms", {"confirm_no": "CF-D1", "supplier_code": "SUP-A", "sku": "X", "dock_day": "2026-11-18", "qty": 1}),
    ]:
        resp = client.post(path, json=payload)
        assert resp.status_code == 409, (path, resp.text)

    conn = connect()
    row = conn.execute(
        "SELECT status, sealed_fingerprint, sealed_match_count, sealed_variance_count "
        "FROM reconcile_batches WHERE id=?",
        (batch["id"],),
    ).fetchone()
    conn.close()
    assert row["status"] == "disputed"
    # 降级保留原封印快照字段，证据页仍可对照排查。
    assert row["sealed_fingerprint"]
    assert row["sealed_match_count"] is not None
    assert row["sealed_variance_count"] is not None

    dash = client.get("/api/dashboard").json()
    assert dash["sealed"] is False
    assert dash["disputed"] is True
    assert dash["lock_issues"]

    detail = client.get(f"/api/reconcile/batches/{batch['id']}").json()
    assert detail["evidence_consistent"] is False
    assert detail["evidence_issues"]

    # 证据修好前禁止第二份现行：重跑、再封、改差额命运全部拒绝。
    r = client.post("/api/reconcile/run", json={"dock_day": "2026-11-18"})
    assert r.status_code == 400
    assert "disputed" in r.json()["detail"]
    r = _seal(client, "2026-11-18", batch["id"], batch["fingerprint"])
    assert r.status_code == 400
    vs = client.get(f"/api/reconcile/variances?batch_id={batch['id']}").json()
    if vs:
        r = client.post(f"/api/reconcile/variances/{vs[0]['id']}/fate", json={"fate": "quarantine"})
        assert r.status_code == 400


def test_concurrent_seal_only_one_current(client):
    """夹具：并发双现行——同日只允许一个 sealed/disputed 锁行。"""
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
    lock_rows = conn.execute(
        "SELECT COUNT(*) c FROM reconcile_batches "
        "WHERE dock_day='2026-11-18' AND status IN ('sealed','disputed')"
    ).fetchone()["c"]
    assert lock_rows == 1
    row = conn.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day='2026-11-18' AND status='sealed'"
    ).fetchone()
    # 没有半截 sealed：快照必须齐全
    assert row["sealed_fingerprint"]
    assert row["sealed_match_count"] is not None
    assert row["sealed_variance_count"] is not None
    # 批头与明细行数必须相等
    detail_total = conn.execute(
        "SELECT COUNT(*) c FROM match_rows WHERE batch_id=?", (row["id"],)
    ).fetchone()["c"]
    assert detail_total == row["matched_count"] + row["variance_count"]
    conn.close()

    # 已封日再跑对账也不得插出第二份现行。
    r = client.post("/api/reconcile/run", json={"dock_day": "2026-11-18"})
    assert r.status_code == 400
    conn = connect()
    currents = conn.execute(
        "SELECT COUNT(*) c FROM reconcile_batches WHERE dock_day='2026-11-18' "
        "AND status IN ('open','sealed','disputed')"
    ).fetchone()["c"]
    conn.close()
    assert currents == 1


def test_dashboard_evidence_counts_match_detail(client):
    """证据摘要不得虚增：总览 sealed_match_count 必须等于批头/明细重算。"""
    batch = _run(client)
    _resolve_all(client)
    assert _seal(client, "2026-11-18", batch["id"], batch["fingerprint"]).status_code == 200

    dash = client.get("/api/dashboard").json()
    detail = client.get(f"/api/reconcile/batches/{batch['id']}").json()
    assert dash["seal_evidence"]["matched_count"] == detail["matched_count"]
    assert dash["seal_evidence"]["variance_count"] == detail["variance_count"]
    assert len(detail["matches"]) == detail["matched_count"] + detail["variance_count"]
    assert len(detail["variances"]) == detail["variance_count"]
    assert detail["evidence_consistent"] is True

def test_migration_clears_legacy_paint_and_rebuilds_lock_index(client):
    """启动迁移负责清除历史假绿标志，并把唯一索引升级为覆盖 disputed。"""
    from app.seed import _migrate

    conn = connect()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('overview_sealed_2026-11-17','1')"
    )
    conn.commit()
    conn.close()

    conn = connect()
    _migrate(conn)
    leftover = conn.execute(
        "SELECT COUNT(*) c FROM settings WHERE key LIKE 'overview_sealed_%'"
    ).fetchone()["c"]
    index_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='ux_reconcile_batches_day_lock'"
    ).fetchone()
    old_index = conn.execute(
        "SELECT COUNT(*) c FROM sqlite_master WHERE name='ux_reconcile_batches_sealed'"
    ).fetchone()["c"]
    conn.close()
    assert leftover == 0
    assert old_index == 0
    assert index_row is not None
    assert "disputed" in index_row["sql"]


def test_leg_write_recheck_blocks_seal_race(client):
    """竞态：旁路闸口已通过、写事务未开时，另一路完成封印；
    事务内复检必须把这次写入挡在 409，正式数量不得落地。"""
    import pytest

    from app.db import transaction
    from app.services import gate

    batch = _run(client)
    _resolve_all(client)

    conn = connect()
    # 闸口在事务外先过：当日确实还没封。
    gate.assert_writable(conn, "2026-11-18")

    # 此刻另一路请求完成结案封印。
    assert _seal(client, "2026-11-18", batch["id"], batch["fingerprint"]).status_code == 200

    # 旁路随后开写事务，事务内复检必须抛出 DayLockedError。
    with pytest.raises(gate.DayLockedError):
        with transaction(conn):
            gate.assert_writable_in_tx(conn, "2026-11-18")
            conn.execute(  # 不应执行到
                "INSERT INTO expect_lines(doc_no, supplier_code, sku, dock_day, qty, created_at) "
                "VALUES ('EX-RACE','SUP-A','X','2026-11-18',1,'2026-11-18T00:00:00Z')"
            )
    conn.close()

    dash = client.get("/api/dashboard").json()
    assert dash["sealed"] is True
    assert dash["expect_count"] == 4


def test_batch_detail_404(client):
    r = client.get("/api/reconcile/batches/424242")
    assert r.status_code == 404
