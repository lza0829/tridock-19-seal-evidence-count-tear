<template>
  <div>
    <div class="hero">
      <div>
        <h1>总览</h1>
        <p>当前到货日三腿行数、对账批与结案状态。结案须绑定未过期 open 批；结案后当日预报/实收/回执不可再改。</p>
      </div>
      <button @click="load">刷新</button>
    </div>
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">到货日</div><div class="v">{{ d.dock_day }}</div></div>
      <div class="stat"><div class="k">预报 / 实收 / 回执</div><div class="v">{{ d.expect_count }} / {{ d.receive_count }} / {{ d.confirm_count }}</div></div>
      <div class="stat"><div class="k">未决差额</div><div class="v">{{ d.pending_variance }}</div></div>
      <div class="stat">
        <div class="k">结案</div>
        <div class="v"><span class="tag" :class="d.sealed ? 'seal' : 'bad'">{{ d.sealed ? '已结案' : '未结案' }}</span></div>
      </div>
    </div>
    <div class="panel" v-if="d && d.sealed && d.seal_evidence">
      <h2>结案证据</h2>
      <p>
        批
        <router-link class="link" :to="`/reconcile/batches/${d.seal_evidence.batch_id}`">{{ d.seal_evidence.batch_no }}</router-link>
        · 对齐 {{ d.seal_evidence.matched_count }} · 差额 {{ d.seal_evidence.variance_count }}
        · {{ d.seal_evidence.sealed_at }} · {{ d.seal_evidence.sealed_by }}
      </p>
      <p class="fp">封印指纹 {{ d.seal_evidence.fingerprint }}</p>
    </div>
    <div class="panel" v-if="d && !d.sealed">
      <h2>待结案批</h2>
      <p v-if="d.latest_batch">
        <router-link class="link" :to="`/reconcile/batches/${d.latest_batch.id}`">{{ d.latest_batch.batch_no }}</router-link>
        · 对齐 {{ d.latest_batch.matched_count }} · 差额 {{ d.latest_batch.variance_count }}
        · {{ d.latest_batch.status }}
        <span class="tag" :class="d.can_seal ? 'ok' : 'bad'">{{ d.can_seal ? '可结案' : '不可结案' }}</span>
      </p>
      <p v-else class="msg">当日无对账批：不能结案，请先到「三联对账」跑批。</p>
    </div>
    <p class="msg">{{ err }}</p>
  </div>
  <p v-if="d && d.sealed && !d.seal_evidence" class="hint">日已封（总览）</p>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const d = ref(null)
const err = ref('')
async function load() {
  err.value = ''
  try { d.value = await api('/dashboard') } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
