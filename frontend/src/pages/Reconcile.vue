<template>
  <div>
    <div class="hero">
      <div>
        <h1>三联对账</h1>
        <p>按供应商+货号+到货日对齐三数量；不一致进差额。结案必须绑定未过期的 open 批（id + 指纹），无批不得封印。</p>
      </div>
    </div>
    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="到货日" />
        <button @click="run">跑三联对账</button>
        <button class="warn" :disabled="!canSeal" @click="seal">结案封印</button>
        <button class="secondary" @click="load">刷新</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
      <p v-if="batch">
        批次 <router-link class="link" :to="`/reconcile/batches/${batch.id}`">{{ batch.batch_no }}</router-link>
        · 对齐 {{ batch.matched_count }} · 差额 {{ batch.variance_count }} · {{ batch.status }}
        <span class="tag" :class="batch.expired ? 'bad' : (batch.status === 'open' ? 'ok' : 'seal')">
          {{ batch.expired ? '已过期' : batch.status }}
        </span>
      </p>
      <p v-else class="msg">当日没有对账批：结案按钮不可用，必须先跑三联对账。</p>
      <p v-if="batch" class="fp">批指纹 {{ batch.fingerprint || '（缺失）' }}
        <span class="tag" :class="batch.fingerprint_valid ? 'ok' : 'bad'">
          {{ batch.fingerprint_valid ? '指纹一致' : '指纹异常' }}
        </span>
      </p>
      <p v-if="batch && batch.expires_at" class="fp">有效期至 {{ batch.expires_at }}（过期后须重跑对账）</p>
    </div>

    <div class="panel" v-if="batches.length">
      <h2>对账批</h2>
      <table>
        <thead><tr><th>批号</th><th>状态</th><th>对齐</th><th>差额</th><th>指纹</th><th>证据</th></tr></thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id">
            <td>{{ b.batch_no }}</td>
            <td>
              <span class="tag" :class="b.status === 'sealed' ? 'seal' : (b.status === 'open' ? 'ok' : 'bad')">
                {{ b.status === 'disputed' ? 'disputed 争议' : b.status }}
              </span>
            </td>
            <td>{{ b.matched_count }}</td>
            <td>{{ b.variance_count }}</td>
            <td class="fp">{{ (b.fingerprint || '').slice(0, 22) }}…</td>
            <td><router-link class="link" :to="`/reconcile/batches/${b.id}`">批详情</router-link></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>对齐行</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>预报</th><th>实收</th><th>回执</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="r in matches" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td><td>{{ r.expect_qty }}</td><td>{{ r.receive_qty }}</td><td>{{ r.confirm_qty }}</td>
            <td><span class="tag" :class="r.aligned ? 'ok' : 'bad'">{{ r.aligned ? '对齐' : '差额' }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>差额处置</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>三数量</th><th>命运</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in variances" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
            <td>{{ r.expect_qty }} / {{ r.receive_qty }} / {{ r.confirm_qty }}</td>
            <td>{{ r.fate }}</td>
            <td>
              <button class="secondary" @click="fate(r.id, 'accept_short')">认短</button>
              <button class="secondary" @click="fate(r.id, 'accept_over')">认溢</button>
              <button class="secondary" @click="fate(r.id, 'quarantine')">隔离</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
const router = useRouter()
const day = ref('2026-11-18')
const batches = ref([])
const batch = ref(null)
const matches = ref([])
const variances = ref([])
const err = ref('')
const ok = ref('')

const current = computed(() =>
  batches.value.find((b) => b.status === 'open') || batches.value[0] || null
)
const canSeal = computed(() =>
  !!batch.value &&
  batch.value.status === 'open' &&
  !batch.value.expired &&
  batch.value.fingerprint_valid
)

async function load() {
  err.value = ''
  batches.value = await api(`/reconcile/batches?dock_day=${encodeURIComponent(day.value)}`)
  batch.value = current.value
  if (!batch.value) { matches.value = []; variances.value = []; return }
  matches.value = await api(`/reconcile/matches?batch_id=${batch.value.id}`)
  variances.value = await api(`/reconcile/variances?batch_id=${batch.value.id}`)
}
async function run() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/run', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '对账完成，已生成待结案批'
    await load()
  } catch (e) { err.value = e.message }
}
async function seal() {
  err.value = ''; ok.value = ''
  if (!batch.value || !batch.value.fingerprint) {
    err.value = '没有可绑定的对账批指纹，拒绝封印'
    return
  }
  try {
    // 结案必须携带批 id 与指纹，服务端会二次核对。
    const sealed = await api('/reconcile/seal', {
      method: 'POST',
      body: JSON.stringify({
        dock_day: day.value,
        batch_id: batch.value.id,
        fingerprint: batch.value.fingerprint,
      }),
    })
    ok.value = `已结案，证据快照 ${sealed.batch_no}`
    await load()
    router.push(`/reconcile/batches/${sealed.id}`)
  } catch (e) { err.value = e.message }
}
async function fate(id, f) {
  err.value = ''; ok.value = ''
  try {
    await api(`/reconcile/variances/${id}/fate`, { method: 'POST', body: JSON.stringify({ fate: f }) })
    ok.value = '已更新命运'
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
