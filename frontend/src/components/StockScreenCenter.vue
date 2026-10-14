<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import { echarts } from '../charts'
import type { StockScreenCandidate, StockScreenDetail, StockScreenSummary } from '../types'

const props = defineProps<{ defaultUniverseSize: number; maxUniverseSize: number; candidateCount: number }>()
const runs = ref<StockScreenSummary[]>([])
const detail = ref<StockScreenDetail | null>(null)
const selectedId = ref('')
const universeSize = ref(props.defaultUniverseSize)
const submitting = ref(false)
const cancelling = ref(false)
const loading = ref(false)
const error = ref('')
const chartHost = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | undefined
let observer: ResizeObserver | undefined
let timer: ReturnType<typeof setInterval> | undefined
let requestSequence = 0

const labels: Record<string, string> = {
  queued: '待扫描', running: '扫描中', completed: '已完成', failed: '失败', cancelled: '已取消',
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '待观察' : `${(Number(value) * 100).toFixed(2)}%`
}

function scorePercent(value: number | null | undefined): string {
  // 未满足对应趋势条件时没有评分；“不适用”与尚未发生的未来收益明确区分。
  return value === null || value === undefined ? '不适用' : `${(Number(value) * 100).toFixed(2)}%`
}

function outcome(item: StockScreenCandidate, horizon: number): string {
  return percent(item.outcomes.find((row) => row.horizon_days === horizon)?.return_rate)
}

function renderChart() {
  if (!chart) return
  const metrics = detail.value?.metrics || []
  const combinations: Array<[string, number, string]> = [
    ['momentum_trend', 5, '动量 · 5日'], ['momentum_trend', 20, '动量 · 20日'],
    ['relative_reversal', 5, '反转 · 5日'], ['relative_reversal', 20, '反转 · 20日'],
  ]
  const values = combinations.map(([code, horizon]) => metrics.find((row) => row.strategy_code === code && row.horizon_days === horizon))
  // 未到观察日使用 null，让 ECharts 留空；不能把缺失值当作 0% 收益。
  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', valueFormatter: (value: number | null) => value === null || !Number.isFinite(Number(value)) ? '待观察' : `${Number(value).toFixed(2)}%` },
    legend: { top: 4, textStyle: { color: '#53655e', fontSize: 13 } },
    grid: { left: 54, right: 20, top: 45, bottom: 36 },
    xAxis: { type: 'category', data: combinations.map((row) => row[2]), axisLabel: { color: '#53655e', fontSize: 12 } },
    yAxis: { type: 'value', axisLabel: { formatter: '{value}%', color: '#64746e' }, splitLine: { lineStyle: { color: '#e4ebe7' } } },
    series: [
      { name: '入选股票等权均值', type: 'bar', data: values.map((row) => row?.mean_return === null || row?.mean_return === undefined ? null : Number(row.mean_return) * 100), itemStyle: { color: '#16805c' }, barMaxWidth: 32 },
      { name: '本批合格股票等权均值', type: 'bar', data: values.map((row) => row?.benchmark_return === null || row?.benchmark_return === undefined ? null : Number(row.benchmark_return) * 100), itemStyle: { color: '#9aa9a2' }, barMaxWidth: 32 },
    ],
  } as echarts.EChartsCoreOption, true)
}

async function loadDetail(id: string) {
  selectedId.value = id
  const sequence = ++requestSequence
  try {
    const result = await api.getStockScreen(id)
    if (sequence === requestSequence) detail.value = result
  } catch (exc) {
    if (sequence === requestSequence) error.value = exc instanceof Error ? exc.message : '读取实验失败'
  }
}

async function refresh() {
  if (loading.value) return
  loading.value = true
  try {
    const items = await api.listStockScreens()
    runs.value = items
    error.value = ''
    const id = items.some((item) => item.run_id === selectedId.value) ? selectedId.value : (items[0]?.run_id || '')
    if (id) await loadDetail(id)
    else detail.value = null
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '读取选股实验失败'
  } finally {
    loading.value = false
  }
}

async function create() {
  if (!Number.isInteger(universeSize.value) || universeSize.value < 1 || universeSize.value > props.maxUniverseSize) {
    error.value = `请输入 1 至 ${props.maxUniverseSize} 只股票`
    return
  }
  submitting.value = true
  error.value = ''
  try {
    const result = await api.createStockScreen(universeSize.value)
    selectedId.value = result.run_id
    await refresh()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '创建扫描失败'
  } finally {
    submitting.value = false
  }
}

async function cancel() {
  if (!detail.value || !['queued', 'running'].includes(detail.value.status)) return
  cancelling.value = true
  try {
    await api.cancelStockScreen(detail.value.run_id)
    await refresh()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '取消扫描失败'
  } finally {
    cancelling.value = false
  }
}

watch(detail, renderChart)
watch(() => props.defaultUniverseSize, (value) => { universeSize.value = value })
onMounted(async () => {
  await nextTick()
  if (chartHost.value) {
    chart = echarts.init(chartHost.value, undefined, { renderer: 'canvas' })
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(chartHost.value)
  }
  await refresh()
  timer = setInterval(() => { void refresh() }, 15000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  observer?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <section class="screen-heading">
    <div><div class="eyebrow"><span class="section-index">07 / STOCK SCREEN</span> 选股实验</div><p>同批股票、两种固定信号；收盘后生成候选，再观察实际 5 / 20 个交易日。</p></div>
    <button class="secondary-button" :disabled="loading" @click="refresh">↻ 刷新结果</button>
  </section>
  <div v-if="error" class="screen-error" role="alert">{{ error }}</div>
  <div class="screen-layout">
    <aside class="screen-side">
      <section class="panel screen-create">
        <span class="eyebrow">NEW SCAN</span>
        <h3>开始一次收盘选股</h3>
        <p>从全市场成交额靠前的股票中，逐只读取历史日线并按固定规则排序。</p>
        <label for="universe-size">扫描股票数量</label>
        <div class="screen-create-row"><input id="universe-size" v-model.number="universeSize" type="number" min="1" :max="maxUniverseSize"><button :disabled="submitting" @click="create">{{ submitting ? '创建中…' : '开始扫描' }}</button></div>
        <small>默认 {{ defaultUniverseSize }} 只 · 每种信号最多 {{ candidateCount }} 只 · 16:00 后可启动</small>
      </section>
      <section class="panel screen-run-list">
        <div class="panel-heading"><div><span class="eyebrow">RUNS</span><h3>扫描记录</h3></div><span class="count-pill">{{ runs.length }}</span></div>
        <button v-for="run in runs" :key="run.run_id" class="screen-run" :class="{ selected: selectedId === run.run_id }" @click="loadDetail(run.run_id)">
          <strong>{{ run.signal_cutoff }} · {{ run.requested_count }} 只</strong><span>{{ labels[run.status] }} · {{ run.processed_count }}/{{ run.requested_count }}</span>
        </button>
        <p v-if="!runs.length" class="screen-muted">尚无扫描记录。首次扫描可能需要较长时间，进度会持续保存。</p>
      </section>
    </aside>

    <div class="screen-main">
      <section class="panel screen-overview">
        <template v-if="detail">
          <div class="panel-heading"><div><span class="eyebrow">RESEARCH RUN · {{ detail.rule_version }}</span><h3>{{ detail.signal_cutoff }} 启动的扫描</h3></div><span class="screen-status">{{ labels[detail.status] }}</span></div>
          <div class="screen-stats"><div><small>扫描进度</small><b>{{ detail.processed_count }} / {{ detail.requested_count }}</b></div><div><small>合格股票</small><b>{{ detail.eligible_count }}</b></div><div><small>数据排除</small><b>{{ detail.excluded_count }}</b></div></div>
          <div class="screen-progress"><span :style="{ width: `${detail.requested_count ? detail.processed_count / detail.requested_count * 100 : 0}%` }"></span></div>
          <p v-if="detail.error_message" class="screen-inline-error">{{ detail.error_message }}</p>
          <button v-if="detail.status === 'queued' || detail.status === 'running'" class="secondary-button" :disabled="cancelling" @click="cancel">取消扫描</button>
        </template>
        <p v-else class="screen-muted">选择一条记录，或开始第一次扫描。</p>
      </section>

      <section class="screen-rules">
        <article class="panel"><span class="eyebrow">RULE 01 · MOMENTUM</span><h3>中期动量 + 趋势</h3><p>剔除最近 20 个交易日，计算此前 120 / 240 日动量；收盘价需高于 120 日均线。分数高者优先。</p></article>
        <article class="panel"><span class="eyebrow">RULE 02 · REVERSAL</span><h3>短期相对反转</h3><p>收盘价高于 60 日均线时，比较最近 5 日收益与本批合格股票均值；相对走弱者优先。</p></article>
      </section>
      <p class="screen-muted screen-references">规则参考 <a href="https://doi.org/10.1111/j.1540-6261.1993.tb04702.x" target="_blank" rel="noopener noreferrer">中期动量研究</a> 与 <a href="https://doi.org/10.1016/j.pacfin.2021.101607" target="_blank" rel="noopener noreferrer">A 股反转研究</a>；这里使用固定的简化信号，不等同于论文原模型。</p>

      <section class="panel screen-chart-panel"><div class="panel-heading"><div><span class="eyebrow">FORWARD OUTCOME</span><h3>未来收益对照</h3></div><span class="screen-muted">等权均值 · 不含交易费用</span></div><div ref="chartHost" class="screen-chart"></div><p class="screen-muted">未来 5 / 20 个有成交日到期后逐股补录；空白代表尚无结果。灰色是本批合格股票的等权均值。</p></section>

      <section class="panel screen-candidates"><div class="panel-heading"><div><span class="eyebrow">CANDIDATES</span><h3>入选股票</h3></div><span class="count-pill">{{ detail?.candidates.length || 0 }}</span></div>
        <div class="screen-table-wrap"><table><thead><tr><th>股票</th><th>信号日</th><th>入选规则 / 排名</th><th>动量分数</th><th>反转分数</th><th>5 日</th><th>20 日</th></tr></thead><tbody>
          <tr v-for="item in detail?.candidates || []" :key="item.symbol"><td><b>{{ item.name }}</b><small>{{ item.symbol }}</small></td><td>{{ item.signal_date }}</td><td><span v-if="item.momentum_rank">动量 #{{ item.momentum_rank }}</span><span v-if="item.reversal_rank">反转 #{{ item.reversal_rank }}</span></td><td>{{ scorePercent(item.momentum_score) }}</td><td>{{ scorePercent(item.reversal_score) }}</td><td>{{ outcome(item, 5) }}</td><td>{{ outcome(item, 20) }}</td></tr>
          <tr v-if="!detail?.candidates.length"><td colspan="7" class="screen-muted">{{ detail?.status === 'running' ? '正在逐只读取历史日线，完成后显示固定排名。' : '当前尚无入选结果。' }}</td></tr>
        </tbody></table></div>
      </section>
      <p class="screen-disclaimer">这是一项前瞻研究实验，排名分数不是上涨概率。筛选与未来收益均基于前复权日线；结果不构成买入建议。</p>
    </div>
  </div>
</template>

<style scoped>
.screen-references{margin:0 2px}.screen-references a{color:#176c4d}
.screen-heading{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:22px}.screen-heading p{margin:8px 0 0;color:#53655e;font-size:14px}.screen-layout{display:grid;grid-template-columns:minmax(250px,300px) minmax(0,1fr);gap:20px}.screen-side,.screen-main{display:grid;align-content:start;gap:18px;min-width:0}.screen-side .panel,.screen-main .panel{padding:22px;min-width:0}.screen-create h3{margin:10px 0;font-size:19px}.screen-create p,.screen-rules p{color:#53655e;font-size:13px;line-height:1.7}.screen-create label{display:block;margin:18px 0 8px;font-size:13px}.screen-create-row{display:flex;gap:8px}.screen-create-row input{width:95px;min-width:0;padding:10px;border:1px solid #d5e0db;border-radius:5px;background:#fff}.screen-create-row button{flex:1;border:0;border-radius:5px;background:#176c4d;color:#fff;cursor:pointer}.screen-create-row button:disabled{opacity:.6;cursor:wait}.screen-create small,.screen-muted,.screen-disclaimer{color:#63746d;font-size:12px;line-height:1.6}.screen-create small{display:block;margin-top:12px}.screen-run{display:grid;gap:7px;width:100%;margin-top:10px;padding:12px;text-align:left;border:1px solid #e0e8e3;border-radius:5px;background:#fff;cursor:pointer}.screen-run.selected{border-color:#63a788;background:#f0f7f2}.screen-run strong{font-size:13px}.screen-run span{color:#65766e;font-size:12px}.screen-status{padding:5px 10px;border-radius:15px;background:#eaf5ee;color:#177651;font-size:12px}.screen-stats{display:flex;gap:26px;margin:18px 0}.screen-stats div{display:grid;gap:4px}.screen-stats small{color:#65766e}.screen-stats b{font-size:20px}.screen-progress{height:6px;overflow:hidden;border-radius:4px;background:#e5eee8}.screen-progress span{display:block;height:100%;background:#17845c;transition:width .3s}.screen-overview button{margin-top:16px}.screen-rules{display:grid;grid-template-columns:1fr 1fr;gap:18px}.screen-rules h3{font-size:17px;margin:10px 0 5px}.screen-chart{width:100%;height:280px}.screen-chart-panel p{margin:0}.screen-table-wrap{overflow:auto;margin-top:16px}.screen-table-wrap table{width:100%;border-collapse:collapse;font-size:13px;min-width:680px}.screen-table-wrap th,.screen-table-wrap td{padding:12px 9px;border-bottom:1px solid #e2eae5;text-align:left}.screen-table-wrap th{color:#6b7c73;font-weight:500;white-space:nowrap}.screen-table-wrap td small{display:block;color:#73837b;margin-top:3px}.screen-table-wrap td span{display:block;white-space:nowrap}.screen-error,.screen-inline-error{padding:10px 12px;border:1px solid #e7b9b4;border-radius:5px;background:#fff2f0;color:#a43f37;font-size:13px;margin-bottom:14px}.screen-inline-error{margin-top:12px;margin-bottom:0}.screen-disclaimer{margin:0 2px 15px}/* 窄屏网格允许收缩，股票表格只在自己的容器里横向滚动。 */@media(max-width:900px){.screen-layout{grid-template-columns:minmax(0,1fr)}.screen-rules{grid-template-columns:minmax(0,1fr)}}
</style>
