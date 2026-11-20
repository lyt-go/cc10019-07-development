<template>
  <section class="page" data-module="behavior_heatmap">
    <header class="page-head">
      <div>
        <h2>行为热力</h2>
        <p class="page-desc">
          读取事件聚合明细生成热力格子，按筛选后的区间重算格值与色阶，并标明每格实际覆盖的时间桶。
          缺覆盖的格子不参与色阶与横向比较，整张图只使用同一个聚合版本。
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
        <span>热力配置</span>
        <select v-model="params.config" data-testid="heatmap-config">
          <option v-for="item in configs" :key="item.config_id" :value="item.config_id">
            {{ item.config_id }} · {{ item.event_id }} · {{ item.spatial_dim }}
          </option>
        </select>
      </label>
      <label><span>开始时间</span><input v-model="params.start" data-testid="heatmap-start" placeholder="09:40" /></label>
      <label><span>结束时间</span><input v-model="params.end" data-testid="heatmap-end" placeholder="10:20" /></label>
      <label><span>聚合版本（可选）</span><input v-model="params.version" data-testid="heatmap-version" placeholder="钉住某个版本" /></label>
      <button class="btn primary" type="submit" data-testid="run-analysis">生成热力</button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="heatmap-error">{{ errorMessage }}</p>

    <div v-if="result" class="heatmap-meta">
      <span data-testid="heatmap-version-label">聚合版本 {{ result.aggregate_version }}</span>
      <span>数据源 {{ result.source_aggregate }}</span>
      <span>色阶 {{ result.scale_min }} ~ {{ result.scale_max }}（{{ scaleSourceLabel }}）</span>
      <span>可比较桶 {{ (result.comparable_buckets || []).join('、') || '无' }}</span>
    </div>

    <div v-if="chartCells.length" class="analysis-chart">
      <div v-for="cell in chartCells" :key="`${cell.dimension}-${cell.time_slot}`" class="analysis-bar" :style="{ height: barHeight(cell.value) + 'px', background: cell.color }">
        <span>{{ cell.time_slot.slice(11, 16) }}</span>
      </div>
    </div>

    <table v-if="result && result.cell_index.length" class="data-table" data-testid="heatmap-table">
      <thead>
        <tr><th>格子</th><th>时间桶</th><th>数值</th><th>覆盖</th><th>来源</th><th>实际覆盖时段</th><th>色阶</th><th>可比较</th></tr>
      </thead>
      <tbody>
        <tr v-for="cell in result.cell_index" :key="`${cell.dimension}-${cell.time_slot}`" :data-testid="`heatmap-cell-${cell.time_slot}`">
          <td>{{ cell.dimension }}</td>
          <td>{{ cell.time_slot }}</td>
          <td>{{ cell.value }}</td>
          <td>{{ cell.coverage_status }}（{{ Math.round(cell.coverage_ratio * 100) }}%）</td>
          <td>{{ sourceLabel(cell.source) }}</td>
          <td>{{ coveredLabel(cell) }}</td>
          <td><span class="swatch" :style="{ background: cell.color }" />{{ cell.color_level }}</td>
          <td>{{ cell.comparable ? '是' : '否' }}</td>
        </tr>
      </tbody>
    </table>

    <section v-if="result && (result.excluded_cells.length || result.unavailable_buckets.length)" class="excluded-panel" data-testid="heatmap-excluded">
      <h3>不参与比较的格子</h3>
      <ul>
        <li v-for="cell in result.excluded_cells" :key="`${cell.dimension}-${cell.time_slot}`">
          {{ cell.dimension }} · {{ cell.time_slot }} · {{ cell.reason }}
        </li>
        <li v-for="bucket in result.unavailable_buckets" :key="bucket.bucket">
          {{ bucket.bucket }} · 整桶无格子 · {{ bucket.reason }}
        </li>
      </ul>
    </section>

    <p v-if="result && !result.cell_index.length" style="text-align:center;color:var(--muted)">
      该区间没有可用的热力格子
    </p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type HeatmapConfig = { config_id: string; event_id: string; spatial_dim: string; time_range: string }
type Cell = {
  dimension: string
  time_slot: string
  value: number
  coverage_status: string
  coverage_ratio: number
  source: string
  color: string
  color_level: number
  comparable: boolean
  covered_buckets: Array<{ bucket: string; covered_start: string; covered_end: string; coverage_status: string; coverage_ratio: number; source: string; exact: boolean }>
}
type HeatmapResult = {
  aggregate_version: string
  source_aggregate: string
  scale_min: number
  scale_max: number
  scale_source: string
  comparable_buckets: string[]
  incomparable_buckets: string[]
  cell_index: Cell[]
  excluded_cells: Array<{ dimension: string; time_slot: string; reason: string }>
  unavailable_buckets: Array<{ bucket: string; coverage_status: string; reason: string }>
  time_window: { covered_buckets: string[] }
}

const ENDPOINT = '/api/behavior-heatmap'
const configs = ref<HeatmapConfig[]>([])
const result = ref<HeatmapResult | null>(null)
const errorMessage = ref('')
const params = ref({ config: 'BH-001', start: '09:40', end: '10:20', version: '' })

const chartCells = computed(() => (result.value?.cell_index ?? []).slice(0, 12))
const scaleSourceLabel = computed(() => {
  if (!result.value) return ''
  if (result.value.scale_source === 'comparable_cells_only') return '仅可比较格子'
  if (result.value.scale_source === 'none') return '无数据'
  return '无可比较格子'
})
const summaryMetrics = computed(() => [
  { label: '覆盖小时桶', value: result.value?.time_window.covered_buckets.length ?? 0, hint: '区间涉及的整点' },
  { label: '可比较格子', value: (result.value?.cell_index ?? []).filter((cell) => cell.comparable).length, hint: '进入色阶与比较' },
  { label: '排除项', value: (result.value?.excluded_cells.length ?? 0) + (result.value?.unavailable_buckets.length ?? 0), hint: '缺覆盖' },
  { label: '聚合版本', value: result.value?.aggregate_version ?? '—', hint: '整图统一版本' },
])

function barHeight(value: number) {
  const values = chartCells.value.map((cell) => cell.value)
  const max = Math.max(...values, 1)
  return Math.max(10, (value / max) * 120)
}
function sourceLabel(source: string) {
  if (source === 'whole_bucket') return '整桶统计'
  if (source === 'minute_detail') return '分钟明细'
  return '不可用'
}
function coveredLabel(cell: Cell) {
  const bucket = cell.covered_buckets[0]
  if (!bucket) return '—'
  return `${bucket.covered_start.slice(11, 16)} - ${bucket.covered_end.slice(11, 16)}`
}

async function loadConfigs() {
  try {
    const data = await fetchJson<{ items: HeatmapConfig[] }>(ENDPOINT)
    configs.value = data.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '配置加载失败'
  }
}

async function runAnalysis() {
  errorMessage.value = ''
  const query = new URLSearchParams({ start: params.value.start, end: params.value.end })
  if (params.value.version) query.set('version', params.value.version)
  try {
    const response = await fetch(`${ENDPOINT}/${params.value.config}/generate?${query.toString()}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    })
    if (!response.ok) throw new Error(`接口返回 ${response.status}，热力未生成`)
    const data = (await response.json()) as { entry: HeatmapResult }
    result.value = data.entry
  } catch (error) {
    result.value = null
    errorMessage.value = error instanceof Error ? error.message : '生成失败'
  }
}

onMounted(loadConfigs)
</script>

<style scoped>
.heatmap-meta { display: flex; flex-wrap: wrap; gap: 12px; font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; }
.excluded-panel { margin-top: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; font-size: 13px; }
.excluded-panel h3 { margin: 0 0 6px; font-size: 13px; }
.excluded-panel ul { margin: 0; padding-left: 18px; color: var(--muted); }
</style>
