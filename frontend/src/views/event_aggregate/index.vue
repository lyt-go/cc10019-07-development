<template>
  <section class="page" data-module="event_aggregate">
    <header class="page-head">
      <div>
        <h2>事件聚合</h2>
        <p class="page-desc">
          按开始/结束时间查询事件聚合明细，逐个小时桶核对覆盖来源；区间只覆盖部分小时且缺原始记录时，不拿整小时总数充当区间值。
        </p>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span style="font-size:12px;color:var(--muted)">{{ m.hint }}</span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label>
        <span>聚合配置</span>
        <select v-model="params.config" data-testid="range-config">
          <option v-for="item in configs" :key="item.config_id" :value="item.config_id">
            {{ item.config_id }} · {{ item.event_id }}
          </option>
        </select>
      </label>
      <label><span>开始时间</span><input v-model="params.start" data-testid="range-start" placeholder="09:40 或 2026-11-15T09:40" /></label>
      <label><span>结束时间</span><input v-model="params.end" data-testid="range-end" placeholder="10:20 或 2026-11-15T10:20" /></label>
      <label><span>聚合版本（可选）</span><input v-model="params.version" data-testid="range-version" placeholder="留空取最新版本" /></label>
      <button class="btn primary" type="submit" data-testid="run-analysis">查询明细</button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="range-error">{{ errorMessage }}</p>

    <div v-if="bucketBars.length" class="analysis-chart">
      <div v-for="bar in bucketBars" :key="bar.label" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px', background: bar.color }">
        <span>{{ bar.label }}</span>
      </div>
    </div>

    <table v-if="coverageRows.length" class="data-table" data-testid="coverage-table">
      <thead>
        <tr><th>小时桶</th><th>覆盖状态</th><th>覆盖率</th><th>数据来源</th><th>可比较</th><th>说明</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="bucket in coverageRows" :key="bucket.bucket" :data-testid="`coverage-${bucket.bucket}`">
          <td>{{ bucket.bucket }}</td>
          <td>{{ bucket.coverage_status }}</td>
          <td>{{ formatRatio(bucket.coverage_ratio) }}</td>
          <td>{{ sourceLabel(bucket.source) }}</td>
          <td>{{ bucket.comparable ? '是' : '否' }}</td>
          <td>{{ bucketNote(bucket.bucket) }}</td>
          <td>
            <button
              v-if="bucket.coverage_status === 'partial'"
              class="link"
              type="button"
              :data-testid="`backfill-${bucket.bucket}`"
              @click="backfill(bucket.bucket)"
            >补录该小时</button>
          </td>
        </tr>
      </tbody>
    </table>

    <table v-if="detailRows.length" class="data-table">
      <thead>
        <tr><th>小时桶</th><th>维度</th><th>聚合值</th><th>覆盖状态</th></tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in detailRows" :key="index">
          <td>{{ row.time_slot }}</td>
          <td>{{ row.dimension }}</td>
          <td>{{ row.agg_value }}</td>
          <td>{{ row.coverage_status }}</td>
        </tr>
      </tbody>
    </table>

    <p v-if="queried && !coverageRows.length && !loading" style="text-align:center;color:var(--muted)">
      该区间没有可展示的聚合明细
    </p>
    <footer class="page-foot">
      <span v-if="result">版本 {{ result.version }} · 可比较桶 {{ result.comparable_buckets.length }} · 不可比较桶 {{ result.incomparable_buckets.length }}</span>
      <span>{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Config = { config_id: string; event_id: string; time_grain: string; detail_retention_hours?: number[]; pending_backfill_hours?: number[] }
type Bucket = {
  bucket: string
  coverage_status: string
  coverage_ratio: number
  source: string
  exact: boolean
  comparable: boolean
  note: string
  rows: Array<Record<string, string | number>>
}
type RangeResult = {
  version: string
  agg_dimensions: string[]
  buckets: Bucket[]
  comparable_buckets: string[]
  incomparable_buckets: string[]
  coverage: Array<{ bucket: string; comparable: boolean }>
}

const ENDPOINT = '/api/event-aggregate'
const configs = ref<Config[]>([])
const result = ref<RangeResult | null>(null)
const loading = ref(false)
const queried = ref(false)
const errorMessage = ref('')
const params = ref({ config: 'EA-004', start: '09:40', end: '10:20', version: '' })

const coverageRows = computed(() => result.value?.buckets ?? [])
const bucketBars = computed(() => {
  const buckets = result.value?.buckets ?? []
  const bars = buckets.map((bucket) => {
    const total = bucket.rows.reduce((sum, row) => sum + Number(row.agg_value ?? 0), 0)
    return {
      label: bucket.bucket.slice(11, 16),
      value: total,
      color: bucket.comparable ? 'var(--brand)' : '#cbd5e1',
    }
  })
  return bars
})
const detailRows = computed(() => {
  const dimensions = result.value?.agg_dimensions ?? []
  const rows: Array<{ time_slot: string; dimension: string; agg_value: number; coverage_status: string }> = []
  for (const bucket of result.value?.buckets ?? []) {
    for (const row of bucket.rows) {
      rows.push({
        time_slot: bucket.bucket.slice(11, 16),
        dimension: dimensions.map((dim) => String(row[dim] ?? '—')).join(' / '),
        agg_value: Number(row.agg_value ?? 0),
        coverage_status: bucket.coverage_status,
      })
    }
  }
  return rows.slice(0, 40)
})
const summaryMetrics = computed(() => {
  const config = configs.value.find((item) => item.config_id === params.value.config)
  const comparable = result.value?.comparable_buckets.length ?? 0
  const incomparable = result.value?.incomparable_buckets.length ?? 0
  return [
    { label: '配置数', value: configs.value.length, hint: '事件聚合配置' },
    { label: '明细小时', value: (config?.detail_retention_hours ?? []).join(',') || '—', hint: '保留分钟明细' },
    { label: '可比较桶', value: comparable, hint: '可参与区间比较' },
    { label: '不可比较桶', value: incomparable, hint: '缺覆盖，已排除' },
  ]
})

function barHeight(value: number) {
  const max = Math.max(...bucketBars.value.map((bar) => bar.value), 1)
  return Math.max(10, (value / max) * 120)
}
function formatRatio(ratio: number) {
  return `${Math.round(ratio * 100)}%`
}
function sourceLabel(source: string) {
  if (source === 'whole_bucket') return '整桶统计'
  if (source === 'minute_detail') return '分钟明细'
  return '不可用'
}
function bucketNote(bucket: string) {
  return coverageRows.value.find((item) => item.bucket === bucket)?.note ?? ''
}

async function loadConfigs() {
  try {
    const data = await fetchJson<{ items: Config[] }>(ENDPOINT)
    configs.value = data.items
    if (data.items.length && !data.items.some((item) => item.config_id === params.value.config)) {
      params.value.config = data.items[0].config_id
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '配置加载失败'
  }
}

async function runAnalysis() {
  loading.value = true
  queried.value = true
  errorMessage.value = ''
  const query = new URLSearchParams({ start: params.value.start, end: params.value.end })
  if (params.value.version) query.set('version', params.value.version)
  try {
    result.value = await fetchJson<RangeResult>(`${ENDPOINT}/${params.value.config}/detail?${query.toString()}`)
  } catch (error) {
    result.value = null
    errorMessage.value = error instanceof Error ? error.message : '查询失败'
  }
  loading.value = false
}

async function backfill(bucket: string) {
  const hour = Number(bucket.slice(11, 13))
  try {
    await request(`${ENDPOINT}/${params.value.config}/backfill?hour=${hour}`, { method: 'POST' })
    await runAnalysis()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补录失败'
  }
}

onMounted(loadConfigs)
</script>
