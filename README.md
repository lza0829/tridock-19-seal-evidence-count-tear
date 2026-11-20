# Tridock（到货三联台）

骨架：到货预报装载 → 实收入账 → 供应商回执装载 → 三方键对齐与差额处置 → 到货日结案封印。

- 前端 http://127.0.0.1:5900
- API http://127.0.0.1:10900

## 结案契约（证据表自洽为唯一事实源）

全局只采用一种口径：**以对账批证据表自洽为准**。日封状态没有第二处事实源，
不存在只刷总览的旁路任务，settings 中任何 `overview_sealed_*` 标志一律无效并在迁移时清理。

1. `POST /api/reconcile/run {dock_day}` 按当前三腿现场生成 open 批，落 `match_rows` /
   `variance_rows`，并计算 `fingerprint`（sha256，覆盖批身份与全部数量明细）。同日旧 open 批
   标记为 `superseded`，不再可结案。当日存在 `sealed` / `disputed` 批时一律拒绝重跑。
2. `POST /api/reconcile/seal {dock_day, batch_id, fingerprint}` 逐项校验：
   - 缺 `batch_id` 或 `fingerprint` → 拒绝（无批不得封印）；
   - 批不存在 / 属于其它 `dock_day` / 已作废 / 已过期 → 拒绝；
   - 请求指纹 ≠ 批指纹，或批指纹与明细重算不一致（明细被改） → 拒绝；
   - **批头计数必须等于按明细重算**（对齐行数、差额行数、`variance_rows` 行数），
     头/明细/内容摘要不齐 → 拒绝，不允许封出缺证据的 sealed；
   - 仍有 `fate='pending'` 的差额 → 拒绝。
3. 封印后批转 `sealed`，同事务原子写入快照 `sealed_by='batch'`、`sealed_fingerprint`、
   `sealed_match_count`、`sealed_variance_count`；部分唯一索引
   `ux_reconcile_batches_day_lock` 保证同日至多一个 `sealed`/`disputed` 锁行，
   `BEGIN IMMEDIATE` 事务兜底并发，插不出第二份现行。
4. 三旁路（预报/实收/回执）共用同一个写闸，不存在任何旁路豁免：
   - 闸口在写事务外执行，写事务内再只读复检，封堵闸口与 INSERT 之间的并发封印；
   - 命中 `sealed`/`disputed` 日一律 **409**；
   - `sealed` 批若在写入时被发现证据不齐（头/明细/指纹/快照任一断裂），
     先在独立事务中降为 `disputed` 再拒写；总览同步显示「证据争议」与问题清单。
5. `disputed` 期间三旁路全拒、不能重跑对账、不能再封、不能改差额命运；
   证据修复前禁止第二份现行。
6. `GET /api/reconcile/batches/{id}` 可打开完整批证据，
   `evidence_consistent` 与 `evidence_issues` 由统一核对函数
   （批头/明细行数/指纹/封印快照）产出；总览摘要计数直接取封印快照，不虚增。

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
