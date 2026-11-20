<template>
  <div>
    <div class="hero">
      <div>
        <h1>批详情</h1>
        <p>批头、封印快照指纹与匹配/差额计数、明细行。结案成功的批次必须能在此打开完整证据。</p>
      </div>
      <div class="row">
        <button class="secondary" @click="load">刷新</button>
        <router-link class="link" to="/reconcile">返回对账</router-link>
      </div>
    </div>
    <p class="msg">{{ err }}</p>
    <template v-if="b">
      <div class="grid">
        <div class="stat"><div class="k">批号</div><div class="v">{{ b.batch_no }}</div></div>
        <div class="stat"><div class="k">到货日</div><div class="v">{{ b.dock_day }}</div></div>
        <div class="stat">
          <div class="k">状态</div>
          <div class="v">
            <span class="tag" :class="b.status === 'sealed' ? 'seal' : (b.status === 'open' ? 'ok' : 'bad')">
              {{ b.status === 'disputed' ? 'disputed（证据争议）' : b.status }}
            </span>
          </div>
        </div>
        <div class="stat"><div class="k">对齐 / 差额</div><div class="v">{{ b.matched_count }} / {{ b.variance_count }}</div></div>
        <div class="stat"><div class="k">明细行数</div><div class="v">{{ b.matches.length }}（应对齐 {{ b.matched_count + b.variance_count }}）</div></div>
      </div>

      <div class="panel">
        <h2>证据指纹</h2>
        <table>
          <tbody>
            <tr><th>当前明细指纹</th><td class="fp">{{ b.fingerprint || '（缺失）' }}</td></tr>
            <tr><th>重算指纹</th><td class="fp">{{ b.recomputed_fingerprint }}</td></tr>
            <tr v-if="b.status === 'sealed' || b.status === 'disputed'">
              <th>封印快照指纹</th><td class="fp">{{ b.sealed_fingerprint || '（缺失）' }}</td></tr>
            <tr v-if="b.status === 'sealed' || b.status === 'disputed'">
              <th>封印快照计数</th><td>对齐 {{ b.sealed_match_count }} · 差额 {{ b.sealed_variance_count }}</td></tr>
            <tr v-if="b.status === 'sealed' || b.status === 'disputed'">
              <th>封印时间 / 来源</th><td>{{ b.sealed_at || '—' }} · {{ b.sealed_by || '—' }}</td></tr>
            <tr v-if="b.status !== 'sealed' && b.status !== 'disputed'">
              <th>有效期</th><td>{{ b.expires_at || '未知' }} <span class="tag" :class="b.expired ? 'bad' : 'ok'">{{ b.expired ? '已过期' : '有效' }}</span></td></tr>
          </tbody>
        </table>
        <p v-if="b.status === 'sealed' || b.status === 'disputed'" class="okmsg" :class="{ msg: b.evidence_consistent === false }">
          {{ b.evidence_consistent ? '证据一致：封印快照与明细吻合' : '证据异常：以下核对项不通过' }}
        </p>
        <ul v-if="b.evidence_consistent === false" class="msg">
          <li v-for="(it, i) in b.evidence_issues" :key="i">{{ it }}</li>
        </ul>
        <p v-if="b.status === 'disputed'" class="msg">
          disputed 期间预报/实收/回执三旁路全拒，且不能重跑对账或再封；须先修复证据。
        </p>
      </div>

      <div class="panel">
        <h2>对齐行（{{ b.matches.length }}）</h2>
        <table>
          <thead><tr><th>供应商</th><th>货号</th><th>预报</th><th>实收</th><th>回执</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="r in b.matches" :key="r.id">
              <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
              <td>{{ r.expect_qty }}</td><td>{{ r.receive_qty }}</td><td>{{ r.confirm_qty }}</td>
              <td><span class="tag" :class="r.aligned ? 'ok' : 'bad'">{{ r.aligned ? '对齐' : '差额' }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h2>差额行（{{ b.variances.length }}）</h2>
        <table>
          <thead><tr><th>供应商</th><th>货号</th><th>预报 / 实收 / 回执</th><th>命运</th></tr></thead>
          <tbody>
            <tr v-for="r in b.variances" :key="r.id">
              <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
              <td>{{ r.expect_qty }} / {{ r.receive_qty }} / {{ r.confirm_qty }}</td>
              <td>{{ r.fate }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>
<script setup>
import { ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
const route = useRoute()
const b = ref(null)
const err = ref('')
async function load() {
  err.value = ''
  try {
    b.value = await api(`/reconcile/batches/${route.params.id}`)
  } catch (e) {
    err.value = e.message
    b.value = null
  }
}
watch(() => route.params.id, load)
onMounted(load)
</script>
