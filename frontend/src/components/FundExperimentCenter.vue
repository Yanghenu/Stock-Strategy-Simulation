<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api/client'
import FundPaperChart from './FundPaperChart.vue'
import type { FundInstrument, FundPaperCreateRequest, FundPaperSession, FundPaperSessionSummary, FundPaperStrategy, FundType } from '../types'
import { fundTypeLabel } from '../utils/fundFormat'
import { fundSearchTerm } from '../utils/fundSearch'

const props = defineProps<{ refreshIntervalSeconds: number; initialCapital: number }>()
const strategies = ref<FundPaperStrategy[]>([])
const tasks = ref<FundPaperSessionSummary[]>([])
// 历史股票类策略保留用于显示旧实验详情，新任务下拉框只展示基金专用策略。
const availableStrategies = computed(() => strategies.value.filter((strategy) => strategy.available_for_new_tasks !== false))
const detail = ref<FundPaperSession | null>(null)
const selectedTaskId = ref('')
const fundType = ref<FundType>('etf')
const query = ref('')
const candidates = ref<FundInstrument[]>([])
const chosenFunds = ref<FundInstrument[]>([])
const parameters = reactive<Record<string, Record<string, number>>>({})
const form = reactive({ task_name: '', strategy_code: 'fund_equal_rebalance', initial_capital: 100000 })
const searching = ref(false)
const loading = ref(true)
const submitting = ref(false)
const stopping = ref(false)
const deleting = ref(false)
const errorMessage = ref('')
const detailError = ref('')
const currentStrategy = computed(() => strategies.value.find((strategy) => strategy.code === form.strategy_code))
const currentParameters = computed(() => parameters[form.strategy_code] || {})
const detailStrategy = computed(() => strategies.value.find((strategy) => strategy.code === detail.value?.strategy_code))
const detailParameterText = computed(() => detail.value && detailStrategy.value
  ? detailStrategy.value.parameters.map((parameter) => `${parameter.label} ${((detail.value as unknown as Record<string, number>)[parameter.key] ?? parameter.default)}${parameter.unit}`).join(' · ')
  : '')
const openPositions = computed(() => detail.value?.funds.filter((fund) => fund.shares > 0) || [])
const profit = computed(() => detail.value ? detail.value.equity - detail.value.initial_capital : 0)
let searchTimer: ReturnType<typeof setTimeout> | undefined
let pollTimer: ReturnType<typeof setInterval> | undefined
let searchSequence = 0
let detailSequence = 0

function money(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? '—' : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(2)}%`
}

function statusLabel(value: string): string {
  return ({ running: '运行中', stopped: '已停止', completed: '已完成', failed: '异常' } as Record<string, string>)[value] || value
}

function sideLabel(value: string): string {
  return ({ buy: '买入', sell: '卖出', dividend: '分红入账' } as Record<string, string>)[value] || value
}

function orderLabel(value: string): string {
  return ({ pending: '待执行', filled: '已成交', cancelled: '已取消' } as Record<string, string>)[value] || value
}

function strategyName(code: string): string {
  return strategies.value.find((strategy) => strategy.code === code)?.name || code
}

async function loadStrategies() {
  strategies.value = await api.getFundPaperStrategies()
  for (const strategy of strategies.value) {
    parameters[strategy.code] = Object.fromEntries(strategy.parameters.map((parameter) => [parameter.key, parameter.default]))
  }
  if (!availableStrategies.value.some((strategy) => strategy.code === form.strategy_code) && availableStrategies.value[0]) {
    form.strategy_code = availableStrategies.value[0].code
  }
}

async function loadTasks(keepDetail = true) {
  try {
    tasks.value = await api.listFundPaperSessions()
    if (!tasks.value.some((item) => item.session_id === selectedTaskId.value)) {
      selectedTaskId.value = tasks.value[0]?.session_id || ''
    }
    if (keepDetail && selectedTaskId.value) await loadDetail(selectedTaskId.value)
    else if (!selectedTaskId.value) detail.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取基金实验任务失败'
  } finally {
    loading.value = false
  }
}

async function loadDetail(sessionId: string) {
  const sequence = ++detailSequence
  try {
    const result = await api.getFundPaperSession(sessionId)
    if (sequence === detailSequence && selectedTaskId.value === sessionId) {
      detail.value = result
      detailError.value = ''
    }
  } catch (error) {
    if (sequence === detailSequence) detailError.value = error instanceof Error ? error.message : '读取实验详情失败'
  }
}

async function searchCandidates() {
  const sequence = ++searchSequence
  const term = fundSearchTerm(query.value)
  if (!term) {
    candidates.value = []
    searching.value = false
    return
  }
  searching.value = true
  errorMessage.value = ''
  try {
    const rows = await api.searchFunds(term, fundType.value)
    if (sequence === searchSequence) candidates.value = rows.filter((item) => !chosenFunds.value.some((chosen) => chosen.symbol === item.symbol))
  } catch (error) {
    if (sequence === searchSequence) errorMessage.value = error instanceof Error ? error.message : '基金搜索失败'
  } finally {
    if (sequence === searchSequence) searching.value = false
  }
}

function addFund(fund: FundInstrument) {
  if (chosenFunds.value.length >= 20 || chosenFunds.value.some((item) => item.symbol === fund.symbol)) return
  chosenFunds.value = [...chosenFunds.value, fund]
  candidates.value = candidates.value.filter((item) => item.symbol !== fund.symbol)
}

function removeFund(symbol: string) {
  const removed = chosenFunds.value.find((item) => item.symbol === symbol)
  chosenFunds.value = chosenFunds.value.filter((item) => item.symbol !== symbol)
  if (removed && (!query.value || `${removed.symbol}${removed.name}`.includes(query.value))) candidates.value.unshift(removed)
}

watch(fundType, () => {
  // 更换基金类型后清空已选项，阻止相同代码的 ETF/开放基金混入一个任务。
  chosenFunds.value = []
  candidates.value = []
  query.value = ''
  void searchCandidates()
})
watch(query, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { void searchCandidates() }, 300)
})
watch(selectedTaskId, (id) => { if (id) void loadDetail(id); else detail.value = null })

async function createTask() {
  if (!form.task_name.trim()) { errorMessage.value = '请填写实验名称'; return }
  if (!chosenFunds.value.length) { errorMessage.value = '请至少选择一支基金'; return }
  if (!Number.isFinite(form.initial_capital) || form.initial_capital <= 0) { errorMessage.value = '初始资金必须大于 0'; return }
  errorMessage.value = ''
  submitting.value = true
  const body: FundPaperCreateRequest = {
    task_name: form.task_name.trim(), fund_type: fundType.value,
    symbols: chosenFunds.value.map((item) => item.symbol), strategy_code: form.strategy_code,
    initial_capital: form.initial_capital, parameters: { ...currentParameters.value },
  }
  try {
    const created = await api.createFundPaperSession(body)
    form.task_name = ''
    chosenFunds.value = []
    candidates.value = []
    await loadTasks(false)
    selectedTaskId.value = created.session_id
    detail.value = created
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建基金实验失败'
  } finally {
    submitting.value = false
  }
}

async function stopTask() {
  if (!detail.value || detail.value.status !== 'running' || stopping.value) return
  stopping.value = true
  detailError.value = ''
  try {
    detail.value = await api.stopFundPaperSession(detail.value.session_id)
    await loadTasks(false)
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : '停止实验失败'
  } finally {
    stopping.value = false
  }
}

async function deleteTask() {
  const task = detail.value
  if (!task || task.status !== 'stopped' || deleting.value) return
  const message = `确认删除基金实验“${task.task_name}”吗？其订单、成交、持仓和净值曲线也会一起删除，且无法恢复。`
  if (!window.confirm(message)) return
  deleting.value = true
  detailError.value = ''
  try {
    await api.deleteFundPaperSession(task.session_id)
    detailSequence += 1
    detail.value = null
    selectedTaskId.value = ''
    await loadTasks(false)
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : '删除实验失败'
  } finally {
    deleting.value = false
  }
}

onMounted(async () => {
  try {
    form.initial_capital = props.initialCapital
    await loadStrategies()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取基金策略失败'
  }
  await loadTasks()
  pollTimer = setInterval(() => { if (!document.hidden) void loadTasks() }, Math.max(10, props.refreshIntervalSeconds) * 1000)
})

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  if (pollTimer) clearInterval(pollTimer)
  searchSequence += 1
  detailSequence += 1
})
</script>

<template>
  <section class="page-heading fund-page-heading">
    <div>
      <div class="eyebrow"><span class="section-index">06 / FUND PAPER</span> 基金实验</div>
      <p>每个任务拥有独立本金与 SQL Server 账本；程序关闭期间不补做交易。</p>
    </div>
    <div class="fund-experiment-count"><span>EXPERIMENTS</span><b>{{ tasks.length.toString().padStart(2, '0') }}</b><small>{{ tasks.filter((task) => task.status === 'running').length }} 个运行中</small></div>
  </section>

  <div class="fund-experiment-layout">
    <div class="fund-experiment-sidebar">
      <section class="panel fund-create-panel">
        <div class="panel-heading"><div><span class="eyebrow">NEW TASK</span><h3>创建基金实验</h3></div></div>
        <form class="fund-create-form" @submit.prevent="createTask">
          <label class="form-field"><span>实验名称</span><input v-model.trim="form.task_name" maxlength="100" placeholder="例如：宽基 ETF 双均线" /></label>
          <div class="form-row">
            <label class="form-field"><span>基金类型</span><select v-model="fundType"><option value="etf">ETF</option><option value="lof">LOF</option><option value="open_end">开放式基金</option></select></label>
            <label class="form-field"><span>初始资金</span><div class="number-field prefix"><small>¥</small><input v-model.number="form.initial_capital" type="number" min="1000" step="1000" /></div></label>
          </div>
          <label class="form-field"><span>搜索基金（{{ chosenFunds.length }}/20）</span><div class="fund-search-inline"><input v-model.trim="query" placeholder="输入代码或名称" @keyup.enter="searchCandidates" /><button type="button" :disabled="searching" @click="searchCandidates">搜索</button></div></label>
          <div class="fund-candidate-list">
            <button v-for="fund in candidates.slice(0, 8)" :key="fund.symbol" type="button" @click="addFund(fund)"><span><b>{{ fund.name }}</b><small>{{ fund.symbol }} · {{ fund.category || fundTypeLabel(fund.fund_type) }}</small></span><i>＋</i></button>
            <div v-if="searching && !candidates.length" class="small-empty">正在搜索…</div>
            <div v-else-if="!candidates.length" class="fund-candidate-empty">{{ query ? '没有可添加的匹配结果。' : '输入代码或名称后搜索基金。' }}</div>
          </div>
          <div v-if="chosenFunds.length" class="chosen-funds"><span v-for="fund in chosenFunds" :key="fund.symbol" class="chosen-fund"><b>{{ fund.symbol }}</b><button type="button" :aria-label="`移除${fund.name}`" @click="removeFund(fund.symbol)">×</button></span></div>
          <label class="form-field"><span>策略</span><select v-model="form.strategy_code"><option v-for="strategy in availableStrategies" :key="strategy.code" :value="strategy.code">{{ strategy.name }}</option></select></label>
          <p v-if="currentStrategy" class="backtest-strategy-description">{{ currentStrategy.description }}</p>
          <div v-if="currentStrategy" class="backtest-strategy-grid fund-parameter-grid">
            <label v-for="parameter in currentStrategy.parameters" :key="parameter.key" class="form-field"><span>{{ parameter.label }}</span><div class="number-field"><input v-model.number="parameters[form.strategy_code][parameter.key]" type="number" :min="parameter.min" :max="parameter.max" :step="parameter.step" /><small>{{ parameter.unit }}</small></div></label>
          </div>
          <p v-if="errorMessage" class="form-error">{{ errorMessage }}</p>
          <button class="primary-button submit-button" :disabled="submitting || !chosenFunds.length || !availableStrategies.length"><span v-if="submitting" class="spinner small"></span>{{ submitting ? '正在创建…' : '开始实验' }}<span v-if="!submitting">↗</span></button>
        </form>
      </section>

      <section class="panel fund-task-list-panel">
        <div class="panel-heading"><div><span class="eyebrow">TASKS</span><h3>实验任务</h3></div><span class="count-pill">{{ tasks.length.toString().padStart(2, '0') }}</span></div>
        <button v-for="task in tasks" :key="task.session_id" type="button" class="fund-task-item" :class="{ selected: selectedTaskId === task.session_id }" @click="selectedTaskId = task.session_id">
          <span class="fund-task-heading"><b>{{ task.task_name }}</b><i :class="task.status">{{ statusLabel(task.status) }}</i></span>
          <span class="fund-task-meta">{{ fundTypeLabel(task.fund_type) }} · {{ strategyName(task.strategy_code) }}</span>
          <span class="fund-task-result"><b :class="task.total_return >= 0 ? 'up' : 'down'">{{ percent(task.total_return) }}</b><small>账户 {{ money(task.equity) }}</small></span>
        </button>
        <div v-if="!tasks.length" class="small-empty">{{ loading ? '正在读取任务…' : '创建第一个基金实验以开始比较。' }}</div>
      </section>
    </div>

    <div v-if="detail" class="fund-task-detail">
      <section class="panel fund-detail-overview">
        <div class="panel-heading"><div><span class="eyebrow">FUND PAPER ACCOUNT</span><h2>{{ detail.task_name }}</h2></div><div class="fund-detail-actions"><span :class="['run-status', detail.status]"><i></i>{{ statusLabel(detail.status) }}</span><button v-if="detail.status === 'running'" class="danger-outline-button" :disabled="stopping" @click="stopTask">{{ stopping ? '正在停止…' : '停止任务' }}</button><button v-else-if="detail.status === 'stopped'" class="danger-outline-button" :disabled="deleting" @click="deleteTask">{{ deleting ? '正在删除…' : '删除任务' }}</button></div></div>
        <div class="fund-detail-caption">{{ strategyName(detail.strategy_code) }} · {{ fundTypeLabel(detail.fund_type) }} · 初始 {{ money(detail.initial_capital) }} · 创建于 {{ new Date(detail.started_at).toLocaleString('zh-CN') }}</div>
        <div class="fund-account-metrics">
          <div><span>账户盈亏</span><b :class="profit >= 0 ? 'up' : 'down'">{{ money(profit) }}</b><small>{{ percent(detail.total_return) }}</small></div>
          <div><span>账户净值</span><b>{{ money(detail.equity) }}</b><small>初始 {{ money(detail.initial_capital) }}</small></div>
          <div><span>可用现金</span><b>{{ money(detail.cash) }}</b><small>独立任务账户</small></div>
          <div><span>持仓市值</span><b>{{ money(detail.market_value) }}</b><small>{{ openPositions.length }} 支持仓</small></div>
          <div><span>最大回撤</span><b class="down">{{ percent(detail.max_drawdown) }}</b><small>峰值至谷值</small></div>
        </div>
        <div class="fund-strategy-note"><b>策略与参数</b><span>{{ detailStrategy?.description || strategyName(detail.strategy_code) }}</span><small>{{ detailParameterText || '默认策略参数' }} · 费率：场内佣金 {{ percent(detail.commission_rate) }}、最低 ¥{{ detail.minimum_commission }}；开放式申购 {{ percent(detail.subscription_fee_rate) }}、赎回 {{ percent(detail.redemption_fee_rate) }}</small></div>
        <div v-if="detail.error_message || detailError" class="form-error fund-detail-error">{{ detailError || detail.error_message }}</div>
      </section>

      <section class="panel chart-panel fund-paper-chart-panel">
        <div class="panel-heading"><div><span class="eyebrow">ACCOUNT EQUITY</span><h3>净值与回撤</h3></div><span class="chart-unit">账户净值 ¥ · 回撤 %</span></div>
        <FundPaperChart :points="detail.equity_curve" />
      </section>

      <section class="panel fund-data-panel">
        <div class="panel-heading"><div><span class="eyebrow">OPEN POSITIONS</span><h3>持有基金</h3></div><span class="count-pill">{{ openPositions.length }}</span></div>
        <div class="table-scroll"><table class="trade-table fund-table"><thead><tr><th>基金</th><th>份额</th><th>最新价格 / 净值</th><th>持仓市值</th><th>持仓盈亏</th><th>成本日期</th></tr></thead><tbody>
          <tr v-for="fund in openPositions" :key="fund.symbol"><td><b>{{ fund.name }}</b><small>{{ fund.symbol }}</small></td><td>{{ fund.shares.toLocaleString('zh-CN', { maximumFractionDigits: 4 }) }}</td><td>{{ fund.last_price?.toFixed(4) ?? '—' }}</td><td>{{ money(fund.shares * (fund.last_price || 0)) }}</td><td :class="fund.shares * (fund.last_price || 0) - fund.cost_basis >= 0 ? 'up' : 'down'">{{ money(fund.shares * (fund.last_price || 0) - fund.cost_basis) }}</td><td>{{ fund.entry_date || '—' }}</td></tr>
          <tr v-if="!openPositions.length"><td colspan="6" class="table-empty">当前没有持仓；策略信号产生后会显示持仓。</td></tr>
        </tbody></table></div>
      </section>

      <section class="panel fund-data-panel">
        <div class="panel-heading"><div><span class="eyebrow">ORDERS</span><h3>订单状态</h3></div><span class="count-pill">{{ detail.orders.length }}</span></div>
        <div class="table-scroll"><table class="trade-table fund-table"><thead><tr><th>基金代码</th><th>方向</th><th>信号日期</th><th>执行日期</th><th>金额 / 份额</th><th>状态</th><th>说明</th></tr></thead><tbody>
          <tr v-for="order in detail.orders.slice(0, 30)" :key="order.order_id || order.idempotency_key"><td>{{ order.symbol }}</td><td><span :class="['side-tag', order.side === 'sell' ? 'down' : '']">{{ sideLabel(order.side) }}</span></td><td>{{ order.signal_date }}</td><td>{{ order.execution_date || '等待新净值' }}</td><td>{{ money(order.amount) }} / {{ order.quantity.toLocaleString('zh-CN', { maximumFractionDigits: 4 }) }}</td><td>{{ orderLabel(order.status) }}</td><td>{{ order.reason }}</td></tr>
          <tr v-if="!detail.orders.length"><td colspan="7" class="table-empty">暂无订单；开放基金将等到下一条已公布净值再成交。</td></tr>
        </tbody></table></div>
      </section>

      <section class="panel fund-data-panel">
        <div class="panel-heading"><div><span class="eyebrow">EXECUTIONS</span><h3>成交与分红</h3></div><span class="count-pill">{{ detail.trades.length }}</span></div>
        <div class="table-scroll"><table class="trade-table fund-table"><thead><tr><th>成交日期</th><th>基金代码</th><th>方向</th><th>成交价 / 净值</th><th>份额</th><th>成交额</th><th>费用</th><th>原因</th></tr></thead><tbody>
          <tr v-for="trade in detail.trades.slice(0, 50)" :key="`${trade.trade_date}-${trade.symbol}-${trade.side}-${trade.signal_date}`"><td>{{ trade.trade_date }}</td><td>{{ trade.symbol }}</td><td><span :class="['side-tag', trade.side === 'sell' ? 'down' : '']">{{ sideLabel(trade.side) }}</span></td><td>{{ trade.execution_price.toFixed(4) }}</td><td>{{ trade.quantity.toLocaleString('zh-CN', { maximumFractionDigits: 4 }) }}</td><td>{{ money(trade.notional) }}</td><td>{{ money(trade.fee) }}</td><td>{{ trade.reason }}</td></tr>
          <tr v-if="!detail.trades.length"><td colspan="8" class="table-empty">暂无成交记录。</td></tr>
        </tbody></table></div>
      </section>
    </div>
    <section v-else class="panel fund-detail-placeholder"><span class="eyebrow">SELECT A TASK</span><h3>选择或创建一个基金实验</h3><p>多个任务分别保存本金、策略参数、订单、持仓和曲线，可并排比较各自结果。</p></section>
  </div>
</template>
