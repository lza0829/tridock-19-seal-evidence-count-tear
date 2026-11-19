# Tridock（到货三联台）

骨架：到货预报装载 → 实收入账 → 供应商回执装载 → 三方键对齐与差额处置 → 到货日结案封印。

- 前端 http://127.0.0.1:5900
- API http://127.0.0.1:10900

## 结案契约（必须绑定对账批）

全局只采用一种口径：**结案必须绑定未过期的 open 对账批**，不允许无批直封。

1. `POST /api/reconcile/run {dock_day}` 按当前三腿现场生成 open 批，落 `match_rows` /
   `variance_rows`，并计算 `fingerprint`（sha256，覆盖批身份与全部数量明细）。同日旧 open 批
   标记为 `superseded`，不再可结案。
2. `POST /api/reconcile/seal {dock_day, batch_id, fingerprint}` 逐项校验：
   - 缺 `batch_id` 或 `fingerprint` → 拒绝（无批不得封印）；
   - 批不存在 / 属于其它 `dock_day` / 已作废 / 已过期 → 拒绝；
   - 请求指纹 ≠ 批指纹，或批指纹与明细重算不一致（明细被改） → 拒绝；
   - 仍有 `fate='pending'` 的差额 → 拒绝。
3. 封印后批转 `sealed`，写入快照 `sealed_by='batch'`、`sealed_fingerprint`、
   `sealed_match_count`、`sealed_variance_count`；同日只允许一个 sealed 批（唯一索引 +
   `BEGIN IMMEDIATE` 事务兜底并发）。
4. 结案后当日三腿写入被写闸拒绝；`GET /api/reconcile/batches/{id}` 可打开完整批证据，
   `evidence_consistent` 标记快照与明细是否吻合。

有效期默认 30 分钟，可用 `TRIDOCK_BATCH_TTL_SECONDS` 调整；过期须重跑对账。
数据目录默认 `./data`，Docker 中用 `TRIDOCK_DATA=/data`。

## 测试

```bash
cd backend
pip install -r requirements.txt pytest httpx
python -m pytest tests -q
```

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10900 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5900
```
