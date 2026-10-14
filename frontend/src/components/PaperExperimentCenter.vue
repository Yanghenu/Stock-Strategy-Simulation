<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api/client'
import PaperTradingChart from './PaperTradingChart.vue'
import StockSearch from './StockSearch.vue'
import StockHistoryPreview from './StockHistoryPreview.vue'
import { paperSymbolPnl } from '../utils/paperExperiment'
import type { Bar, PaperSession, PaperSessionSummary, PaperStrategy, Quote } from '../types'

const props = defineProps<{ refreshIntervalSeconds: number; initialCapital: number }>()
const strategies = ref<PaperStrategy[]>([])
const sessions = ref<PaperSessionSummary[]>([])
const selectedSessionId = ref('')
const selectedSession = ref<PaperSession | null>(null)
const selectedSymbols = ref<Array<Pick<Quote, 'symbol' | 'name'>>>([])
const strategyParameterValues = reactive<Record<string, Record<string, number>>>({})
const symbolQuery = ref('')
const searchResults = ref<Quote[]>([])
const searching = ref(false)
const refreshing = ref(false)
const submitting = ref(false)
const stopping = ref(false)
const errorMessage = ref('')
const previewTarget = ref<{ source: 'form' | 'task'; symbol: string; name: string } | null>(null)
const previewBars = ref<Bar[]>([])
const previewLoading = ref(false)
const previewError = ref('')
const form = reactive({ task_name: '', strategy_code: 'breakout_ma_atr', initial_capital: 100000 })
const capitalTouched = ref(false)
let searchTimer: ReturnType<typeof setTimeout> | undefined
let refreshTimer: ReturnType<typeof setInterval> | undefined
let searchSequence = 0
// 用户快速切换任务时，仅允许最后一次详情请求更新页面，避免旧响应覆盖新任务。
let detailSequence = 0
// 列表请求可能交错返回；只允许最后发起的刷新更新任务列表与选中项。
let sessionListSequence = 0
// 股票预览只允许最后一次点击的请求更新图表，避免快速切换时旧数据闪回。
let previewSequence = 0

const selectedStrategy = computed(() => strategies.value.find((item) => item.code === form.strategy_code))
const sessionStrategy = computed(() => strategies.value.find((item) => item.code === selectedSession.value?.strategy_code))
const currentStrategyParameters = computed(() => strategyParameterValues[form.strategy_code] || {})
const parameterValidationError = computed(() => {
  const strategy = selectedStrategy.value
  if (!strategy) return '正在读取策略参数'
  const values = currentStrategyParameters.value
  for (const parameter of strategy.parameters) {
    const value = values[parameter.key]
    if (!Number.isFinite(value)) return `请填写${parameter.label}`
    if (value < parameter.min || value > parameter.max) {
      return `${parameter.label}需在 ${parameter.min} 到 ${parameter.max} 之间`
    }
    if (parameter.type === 'integer' && !Number.isInteger(value)) return `${parameter.label}必须是整数`
  }
  if (strategy.code === 'dual_ma' && values.short_ma_period >= values.long_ma_period) {
    return '短周期必须小于长周期'
  }
  return ''
})
const canCreate = computed(() => Boolean(
  form.task_name.trim() && selectedSymbols.value.length > 0 && selectedStrategy.value
  && form.initial_capital > 0 && !parameterValidationError.value,
))

function amount(value: number | null | undefined): string {
  return value === null || value === undefined
    ? '—'
    : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(2)}%`
}

function timeLabel(value: string | null): string {
  return value ? value.replace('T', ' ').slice(0, 16) : '无自动到期'
}

function statusLabel(status: string): string {
  return ({ running: '运行中', stopped: '已停止', completed: '已完成', failed: '异常' } as Record<string, string>)[status] || status
}

function strategyName(code: string): string {
  return strategies.value.find((item) => item.code === code)?.name || code
}

function savedParameterSummary(strategyCode: string, session: PaperSession): string {
  const strategy = strategies.value.find((item) => item.code === strategyCode)
  if (!strategy?.parameters.length) return '该策略无需额外参数'
  return strategy.parameters.map((parameter) => (
    `${parameter.label} ${session[parameter.key]} ${parameter.unit}`
  )).join(' · ')
}

function dateOnly(value: Date): string {
  // 使用本地日历日构造行情日期，避免 UTC 转换导致区间首尾偏移一天。
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function closeStockPreview() {
  previewSequence += 1
  previewTarget.value = null
  previewBars.value = []
  previewLoading.value = false
  previewError.value = ''
}

async function toggleStockPreview(stock: Pick<Quote, 'symbol' | 'name'>, source: 'form' | 'task') {
  if (previewTarget.value?.symbol === stock.symbol && previewTarget.value.source === source) {
    closeStockPreview()
    return
  }
  const sequence = ++previewSequence
  previewTarget.value = { ...stock, source }
  previewBars.value = []
  previewLoading.value = true
  previewError.value = ''
  const end = new Date()
  end.setDate(end.getDate() - 1)
  const start = new Date(end)
  start.setDate(start.getDate() - 90)
  try {
    const bars = await api.getBars(stock.symbol, dateOnly(start), dateOnly(end))
    if (sequence === previewSequence) previewBars.value = bars.slice(-30)
  } catch (error) {
    if (sequence === previewSequence) {
      previewError.value = error instanceof Error ? error.message : '读取近期日线失败'
    }
  } finally {
    if (sequence === previewSequence) previewLoading.value = false
  }
}

async function loadSelectedSession(sessionId: string) {
  const sequence = ++detailSequence
  if (!sessionId) {
    selectedSession.value = null
    return
  }
  const session = await api.getPaperSession(sessionId)
  if (sequence === detailSequence && selectedSessionId.value === sessionId) {
    selectedSession.value = session
  }
}

async function refreshData(preferredSessionId?: string) {
  const sequence = ++sessionListSequence
  refreshing.value = true
  try {
    const nextSessions = await api.listPaperSessions()
    if (sequence !== sessionListSequence) return
    sessions.value = nextSessions
    if (preferredSessionId && sessions.value.some((item) => item.session_id === preferredSessionId)) {
      selectedSessionId.value = preferredSessionId
    }
    if (!sessions.value.some((item) => item.session_id === selectedSessionId.value)) {
      selectedSessionId.value = sessions.value[0]?.session_id || ''
    }
    await loadSelectedSession(selectedSessionId.value)
    if (sequence === sessionListSequence) errorMessage.value = ''
  } catch (error) {
    if (sequence === sessionListSequence) {
      errorMessage.value = error instanceof Error ? error.message : '读取实验任务失败'
    }
  } finally {
    if (sequence === sessionListSequence) refreshing.value = false
  }
}

async function selectSession(sessionId: string) {
  selectedSessionId.value = sessionId
  selectedSession.value = null
  try {
    await loadSelectedSession(sessionId)
    if (selectedSessionId.value === sessionId) errorMessage.value = ''
  } catch (error) {
    if (selectedSessionId.value === sessionId) {
      errorMessage.value = error instanceof Error ? error.message : '读取任务详情失败'
    }
  }
}

function addSymbol(quote: Quote) {
  if (selectedSymbols.value.some((item) => item.symbol === quote.symbol)) return
  if (selectedSymbols.value.length >= 20) {
    errorMessage.value = '每个任务最多选择 20 只股票'
    return
  }
  selectedSymbols.value.push({ symbol: quote.symbol, name: quote.name })
  symbolQuery.value = ''
  searchResults.value = []
  errorMessage.value = ''
}

function removeSymbol(symbol: string) {
  selectedSymbols.value = selectedSymbols.value.filter((item) => item.symbol !== symbol)
  if (previewTarget.value?.source === 'form' && previewTarget.value.symbol === symbol) closeStockPreview()
}

async function createSession() {
  if (!canCreate.value || submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    const created = await api.createPaperSession({
      task_name: form.task_name.trim(),
      symbols: selectedSymbols.value.map((item) => item.symbol),
      strategy_code: form.strategy_code,
      initial_capital: form.initial_capital,
      parameters: { ...currentStrategyParameters.value },
    })
    form.task_name = ''
    selectedSymbols.value = []
    selectedSessionId.value = created.session_id
    selectedSession.value = null
    await refreshData(created.session_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建纸面实验失败'
  } finally {
    submitting.value = false
  }
}

async function stopSession() {
  const sessionId = selectedSession.value?.session_id
  if (!sessionId || selectedSession.value?.status !== 'running' || stopping.value) return
  stopping.value = true
  errorMessage.value = ''
  try {
    await api.stopPaperSession(sessionId)
    // 请求期间若用户切换任务，只更新发起停止的任务；列表仍刷新但保留当前选择。
    if (selectedSessionId.value === sessionId && selectedSession.value?.session_id === sessionId) {
      selectedSession.value.status = 'stopped'
    }
    await refreshData()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '停止任务失败'
  } finally {
    stopping.value = false
  }
}

watch(symbolQuery, (value) => {
  if (searchTimer) clearTimeout(searchTimer)
  // 每次输入都递增序号；较慢的旧请求返回时不能覆盖用户刚输入的新结果。
  const sequence = ++searchSequence
  const query = value.trim()
  if (query.length < 2) {
    searchResults.value = []
    searching.value = false
    return
  }
  searchTimer = setTimeout(async () => {
    searching.value = true
    try {
      const matches = await api.searchStocks(query)
      if (sequence === searchSequence) searchResults.value = matches
    } catch (error) {
      if (sequence === searchSequence) {
        errorMessage.value = error instanceof Error ? error.message : '股票搜索失败'
        searchResults.value = []
      }
    } finally {
      if (sequence === searchSequence) searching.value = false
    }
  }, 300)
})

watch(() => props.initialCapital, (value) => {
  // 页面初始资金可以随后从配置加载；用户编辑过金额后则保留其输入。
  if (!capitalTouched.value) form.initial_capital = value
}, { immediate: true })

onMounted(async () => {
  try {
    strategies.value = await api.getPaperStrategies()
    for (const strategy of strategies.value) {
      strategyParameterValues[strategy.code] = Object.fromEntries(
        strategy.parameters.map((parameter) => [parameter.key, parameter.default]),
      )
    }
    if (!strategies.value.some((item) => item.code === form.strategy_code)) {
      form.strategy_code = strategies.value[0]?.code || ''
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取策略目录失败'
  }
  await refreshData()
  // 复用配置中的行情刷新频率；页面隐藏时跳过查询，减少空转请求。
  refreshTimer = setInterval(() => {
    if (!document.hidden && !refreshing.value) void refreshData()
  }, Math.max(1, props.refreshIntervalSeconds) * 1000)
})

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  if (refreshTimer) clearInterval(refreshTimer)
  // 使卸载前尚未完成的搜索响应失效，避免回写已离开的页面状态。
  searchSequence += 1
  previewSequence += 1
})
</script>

<template>
  <div class="paper-center">
    <section class="page-heading paper-page-heading">
      <div>
        <div class="eyebrow"><span class="section-index">03 / PAPER</span> 多任务纸面实验</div>
        <p>各任务拥有独立股票池、资金和账本；服务停止期间不交易，重启后只处理新行情。</p>
      </div>
      <button class="secondary-button" :disabled="refreshing" @click="() => refreshData()">
        <span :class="{ rotating: refreshing }">↻</span> 刷新任务
      </button>
    </section>

    <div v-if="errorMessage" class="form-error paper-center-error" role="alert">{{ errorMessage }}</div>

    <section class="panel experiment-create-panel">
      <div class="panel-heading">
        <div><span class="eyebrow">NEW PAPER TASK</span><h3>创建实验任务</h3></div>
        <span class="count-pill">{{ selectedSymbols.length }} / 20 只股票</span>
      </div>
      <div class="experiment-form-grid">
        <label class="experiment-field">
          <span>任务名称</span>
          <input v-model="form.task_name" maxlength="100" placeholder="例如：银行股双均线策略" />
        </label>
        <label class="experiment-field">
          <span>初始资金</span>
          <div class="capital-input"><b>¥</b><input v-model.number="form.initial_capital" type="number" min="1" step="1000" @input="capitalTouched = true" /></div>
        </label>
        <div class="experiment-field symbol-field">
          <span>股票池</span>
          <div class="stock-picker">
            <StockSearch v-model="symbolQuery" :results="searchResults" :searching="searching" @select="addSymbol" />
          </div>
          <div class="selected-symbols">
            <span v-for="item in selectedSymbols" :key="item.symbol" class="symbol-selection">
              <button
                type="button"
                :class="['symbol-chip', { active: previewTarget?.source === 'form' && previewTarget.symbol === item.symbol }]"
                :aria-expanded="previewTarget?.source === 'form' && previewTarget.symbol === item.symbol"
                @click="toggleStockPreview(item, 'form')"
              >
                <b>{{ item.symbol }}</b> {{ item.name }} <small>近期数据</small>
              </button>
              <button type="button" class="symbol-remove" :aria-label="`移除 ${item.name}`" @click="removeSymbol(item.symbol)">×</button>
            </span>
            <small v-if="!selectedSymbols.length">搜索代码或名称，再从结果中添加。</small>
          </div>
          <StockHistoryPreview
            v-if="previewTarget?.source === 'form'"
            :symbol="previewTarget.symbol"
            :name="previewTarget.name"
            :bars="previewBars"
            :loading="previewLoading"
            :error="previewError"
            @close="closeStockPreview"
          />
        </div>
        <div class="experiment-field strategy-field">
          <span>使用策略</span>
          <select v-model="form.strategy_code">
            <option v-for="strategy in strategies" :key="strategy.code" :value="strategy.code">{{ strategy.name }}</option>
          </select>
          <p v-if="selectedStrategy" class="strategy-description">{{ selectedStrategy.description }}</p>
          <p v-if="form.strategy_code === 'defensive_mean_reversion'" class="strategy-description">
            长期趋势过滤有效且短期超卖时入场，按波动率缩放仓位并受最大仓位限制；每只股票使用独立风险额度，未投入部分保留现金，不跨股票轮动挪用。停机期间错过的信号不会补单。
          </p>
          <div v-if="selectedStrategy" class="strategy-parameters">
            <div class="parameter-heading"><b>策略参数</b><small>切换策略后可分别调整</small></div>
            <div v-if="selectedStrategy.parameters.length" class="strategy-parameter-grid">
              <label v-for="parameter in selectedStrategy.parameters" :key="parameter.key" class="parameter-field">
                <span>{{ parameter.label }} <small>{{ parameter.unit }}</small></span>
                <input
                  v-model.number="currentStrategyParameters[parameter.key]"
                  type="number"
                  :min="parameter.min"
                  :max="parameter.max"
                  :step="parameter.step"
                  :aria-label="parameter.label"
                />
                <small>{{ parameter.description }}（{{ parameter.min }}–{{ parameter.max }}）</small>
              </label>
            </div>
            <p v-else class="no-strategy-parameters">该策略无需额外参数。</p>
            <p v-if="parameterValidationError" class="parameter-error" role="alert">{{ parameterValidationError }}</p>
          </div>
        </div>
      </div>
      <div v-if="selectedStrategy" class="strategy-defaults">
        <span>每只股票按等额预算，100 股整数手；参数和费用在创建时保存</span>
      </div>
      <div class="create-task-actions">
        <small>之后修改参数不会改变已运行任务。</small>
        <button class="primary-button" :disabled="!canCreate || submitting" @click="createSession">
          {{ submitting ? '正在创建…' : '开始纸面实验' }} <span v-if="!submitting">↗</span>
        </button>
      </div>
    </section>

    <div class="experiment-layout">
      <aside class="panel experiment-task-panel">
        <div class="panel-heading"><div><span class="eyebrow">TASKS</span><h3>实验任务</h3></div><span class="count-pill">{{ sessions.length }}</span></div>
        <div v-if="sessions.length" class="experiment-task-list">
          <button
            v-for="task in sessions"
            :key="task.session_id"
            type="button"
            :class="['experiment-task-card', { selected: selectedSessionId === task.session_id }]"
            @click="selectSession(task.session_id)"
          >
            <span class="task-card-heading"><b>{{ task.task_name }}</b><i :class="['task-status', task.status]">{{ statusLabel(task.status) }}</i></span>
            <small>{{ strategyName(task.strategy_code) }} · {{ task.symbol_count }} 只</small>
            <span class="task-card-metrics">
              <span><small>收益</small><b :class="task.total_return >= 0 ? 'up' : 'down'">{{ percent(task.total_return) }}</b></span>
              <span><small>最大回撤</small><b class="down">{{ percent(task.max_drawdown) }}</b></span>
              <span><small>账户净值</small><b>{{ amount(task.equity) }}</b></span>
            </span>
          </button>
        </div>
        <div v-else class="task-list-empty">暂无任务。创建实验后，可在这里比较不同策略与股票池。</div>
      </aside>

      <div v-if="selectedSession" class="experiment-detail-column">
        <section class="panel experiment-overview-panel">
          <div class="panel-heading">
            <div><span class="eyebrow">PAPER ACCOUNT</span><h3>{{ selectedSession.task_name }}</h3></div>
            <div class="detail-heading-actions">
              <i :class="['task-status', selectedSession.status]">{{ statusLabel(selectedSession.status) }}</i>
              <button v-if="selectedSession.status === 'running'" class="stop-task-button" :disabled="stopping" @click="stopSession">{{ stopping ? '正在停止…' : '停止任务' }}</button>
            </div>
          </div>
          <div class="task-caption">
            <span>{{ strategyName(selectedSession.strategy_code) }}</span>
            <span>{{ timeLabel(selectedSession.started_at) }} — {{ timeLabel(selectedSession.ends_at) }}</span>
            <span>{{ selectedSession.trades.length }} 笔成交</span>
          </div>
          <div v-if="selectedSession.error_message" class="form-error task-error">{{ selectedSession.error_message }}</div>
          <div class="task-overview-metrics">
            <div class="metric-cell primary-metric"><span>账户盈亏</span><b :class="selectedSession.equity >= selectedSession.initial_capital ? 'up' : 'down'">{{ amount(selectedSession.equity - selectedSession.initial_capital) }}</b><small>{{ percent(selectedSession.total_return) }}</small></div>
            <div class="metric-cell"><span>账户净值</span><b>{{ amount(selectedSession.equity) }}</b><small>初始 {{ amount(selectedSession.initial_capital) }}</small></div>
            <div class="metric-cell"><span>可用现金</span><b>{{ amount(selectedSession.cash) }}</b><small>独立任务账户</small></div>
            <div class="metric-cell"><span>持仓市值</span><b>{{ amount(selectedSession.market_value) }}</b><small>最后有效报价估值</small></div>
            <div class="metric-cell"><span>最大回撤</span><b class="down">{{ percent(selectedSession.max_drawdown) }}</b><small>峰值到谷值</small></div>
          </div>
          <div v-if="sessionStrategy" class="task-strategy-note">
            <b>策略规则</b>
            <span>{{ sessionStrategy.description }}</span>
            <small>本任务参数：{{ savedParameterSummary(selectedSession.strategy_code, selectedSession) }}</small>
            <small v-if="selectedSession.strategy_code === 'defensive_mean_reversion'">长期趋势过滤有效且短期超卖时入场，按波动率控制仓位；每只股票使用独立风险额度，未投入部分保留现金，不跨股票轮动挪用额度。停机期间错过的信号不补单。</small>
            <small v-if="selectedSession.strategy_code === 'momentum_rotation'">最近调仓：{{ timeLabel(selectedSession.last_rebalanced_at) }}</small>
          </div>
        </section>

        <section class="panel chart-panel paper-chart-panel">
          <div class="panel-heading"><div><span class="eyebrow">EQUITY & DRAWDOWN</span><h3>净值与回撤</h3></div><span class="chart-unit">账户净值 ¥ · 回撤 %</span></div>
          <PaperTradingChart :session="selectedSession" />
        </section>

        <section class="panel archive-panel paper-holdings-panel">
          <div class="panel-heading"><div><span class="eyebrow">PORTFOLIO</span><h3>股票池与持仓盈亏</h3></div><span class="count-pill">{{ selectedSession.symbols.length }}</span></div>
          <div class="archive-table-scroll">
            <table class="archive-table paper-table">
              <thead><tr><th>股票</th><th>等额预算</th><th>持仓股数</th><th>买入成本</th><th>最新价格</th><th>持仓市值</th><th>浮动盈亏</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="item in selectedSession.symbols" :key="item.symbol">
                  <td>
                    <button
                      type="button"
                      class="stock-open-button"
                      :aria-expanded="previewTarget?.source === 'task' && previewTarget.symbol === item.symbol"
                      @click="toggleStockPreview(item, 'task')"
                    >
                      <b>{{ item.symbol }}</b><small>{{ item.name }} · 近期数据</small>
                    </button>
                  </td>
                  <td>{{ amount(item.budget) }}</td>
                  <td>{{ item.shares.toLocaleString('zh-CN') }}</td>
                  <td>{{ item.entry_price ? amount(item.entry_price) : '—' }}</td>
                  <td>{{ item.last_price ? amount(item.last_price) : '—' }}</td>
                  <td>{{ amount(item.shares * (item.last_price ?? 0)) }}</td>
                  <td :class="paperSymbolPnl(item) >= 0 ? 'up' : 'down'">{{ item.shares ? amount(paperSymbolPnl(item)) : '—' }}</td>
                  <td><span :class="['archive-status', item.shares ? 'completed' : '']">{{ item.shares ? '持仓中' : '等待信号' }}</span></td>
                </tr>
                <tr v-if="!selectedSession.symbols.length"><td colspan="8" class="table-empty">任务没有保存股票。</td></tr>
              </tbody>
            </table>
          </div>
          <StockHistoryPreview
            v-if="previewTarget?.source === 'task'"
            :symbol="previewTarget.symbol"
            :name="previewTarget.name"
            :bars="previewBars"
            :loading="previewLoading"
            :error="previewError"
            @close="closeStockPreview"
          />
        </section>

        <section class="panel trades-panel paper-trades-panel">
          <div class="panel-heading"><div><span class="eyebrow">EXECUTIONS</span><h3>模拟成交</h3></div><span class="count-pill">{{ selectedSession.trades.length }}</span></div>
          <div class="archive-table-scroll">
            <table class="archive-table paper-table">
              <thead><tr><th>信号日期</th><th>行情报价执行时间</th><th>股票</th><th>方向</th><th>成交价</th><th>数量</th><th>费用</th><th>触发规则</th></tr></thead>
              <tbody>
                <tr v-for="(trade, index) in [...selectedSession.trades].reverse()" :key="`${trade.symbol}-${trade.trade_date}-${index}`">
                  <td>{{ trade.signal_date ? trade.signal_date.slice(0, 10) : '—' }}</td><td>{{ timeLabel(trade.trade_date) }}</td><td>{{ trade.symbol }}</td>
                  <td><span :class="['side-tag', trade.side === 'sell' ? 'down' : '']">{{ trade.side === 'buy' ? '买入' : '卖出' }}</span></td>
                  <td>{{ amount(trade.price) }}</td><td>{{ trade.quantity.toLocaleString('zh-CN') }}</td>
                  <td>{{ amount(trade.commission + trade.stamp_duty) }}</td><td class="muted">{{ trade.reason }}</td>
                </tr>
                <tr v-if="!selectedSession.trades.length"><td colspan="8" class="table-empty">暂时没有成交；没有满足策略条件时，资金会保留为现金。</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>
      <section v-else class="panel empty-task-detail">
        <div class="eyebrow">TASK DETAIL</div>
        <h3>{{ refreshing ? '正在读取任务…' : '先创建一个实验任务' }}</h3>
        <p>任务启动后会按所选策略读取新行情，独立保存现金、持仓、盈亏和交易曲线。</p>
      </section>
    </div>

    <section class="panel paper-operation-note">
      <span class="eyebrow">模拟范围</span>
      <p>只进行纸面模拟，不连接券商。程序关闭时不操作；重启后不补做停机期间信号。当前撮合按工作日连续竞价时间、100 股整数手和 A 股 T+1 规则执行，不模拟涨跌停排队、停牌成交或分红。</p>
    </section>
  </div>
</template>

<style scoped>
.paper-center { display: grid; gap: 16px; }
.paper-page-heading { margin-bottom: 0; }
.paper-page-heading h1 { max-width: 780px; }
.paper-center-error { margin: 0; }
.experiment-create-panel, .experiment-task-panel, .experiment-overview-panel, .empty-task-detail { padding: 19px 21px; }
.experiment-create-panel .panel-heading, .experiment-task-panel .panel-heading { margin-bottom: 17px; }
.experiment-form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px 20px; }
.experiment-field { min-width: 0; display: grid; align-content: start; gap: 8px; color: #56675f; font-size: 13px; }
.experiment-field input, .experiment-field select { width: 100%; height: 42px; padding: 0 12px; border: 1px solid #d5e0db; border-radius: 5px; background: #fff; color: #263630; font: inherit; }
.experiment-field input:focus, .experiment-field select:focus { outline: 2px solid rgba(20, 128, 92, .22); border-color: #78aa92; }
.capital-input { position: relative; }
.capital-input b { position: absolute; left: 12px; top: 11px; color: #5e7067; font-weight: 500; }
.capital-input input { padding-left: 31px; }
.symbol-field { grid-column: 1; }
.stock-picker :deep(.stock-search) { width: 100%; }
.selected-symbols { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; min-height: 28px; }
.selected-symbols > small { color: #73827b; font-size: 12px; }
.symbol-selection { display: inline-flex; align-items: stretch; overflow: hidden; border: 1px solid #d8e5de; border-radius: 4px; background: #f2f8f4; }
.symbol-chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 7px; border: 0; background: transparent; color: #41534b; font: inherit; font-size: 12px; cursor: pointer; }
.symbol-chip:hover, .symbol-chip.active { background: #e5f2eb; color: #176f52; }
.symbol-chip b { color: #14805c; font-family: var(--mono); }
.symbol-chip small { color: #74827b; font-size: 10px; }
.symbol-remove { width: 28px; border: 0; border-left: 1px solid #d8e5de; background: transparent; color: #61736a; cursor: pointer; font-size: 17px; line-height: 1; }
.symbol-remove:hover { color: #bf514b; background: #f9e9e7; }
.strategy-field { grid-column: 2; grid-row: 1 / span 2; }
.strategy-description { margin: 2px 0 0; color: #687970; font-size: 12px; line-height: 1.6; }
.strategy-parameters { margin-top: 12px; padding: 11px; border: 1px solid #e1eae5; border-radius: 4px; background: #f7faf8; }
.parameter-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 9px; color: #405148; font-size: 12px; }
.parameter-heading small { color: #7a8881; font-size: 10px; }
.strategy-parameter-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 10px; }
.parameter-field { display: grid; gap: 5px; min-width: 0; color: #5b6c63; font-size: 11px; }
.parameter-field > span { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; }
.parameter-field > span small, .parameter-field > small { color: #7c8983; font-size: 10px; }
.parameter-field input { width: 100%; height: 34px; padding: 0 8px; border: 1px solid #d5e0db; border-radius: 4px; background: #fff; color: #263630; font: 12px var(--mono); }
.parameter-field input:focus { outline: 2px solid rgba(20, 128, 92, .18); border-color: #78aa92; }
.parameter-error { margin: 9px 0 0; color: #b34a43; font-size: 11px; }
.no-strategy-parameters { margin: 0; color: #718078; font-size: 11px; }
.strategy-defaults { display: flex; flex-wrap: wrap; gap: 8px 20px; margin-top: 12px; padding: 10px 12px; border: 1px solid #e1eae5; border-radius: 4px; background: #f7faf8; color: #50635a; font-size: 12px; }
.create-task-actions { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 18px; }
.create-task-actions small { color: #718078; font-size: 12px; }
.create-task-actions .primary-button { min-width: 170px; }
.experiment-layout { display: grid; grid-template-columns: minmax(265px, 310px) minmax(0, 1fr); align-items: start; gap: 16px; }
.experiment-task-panel { position: sticky; top: 92px; }
.experiment-task-list { display: grid; gap: 9px; }
.experiment-task-card { width: 100%; padding: 12px; border: 1px solid #dfe8e3; border-radius: 5px; background: #fff; text-align: left; cursor: pointer; transition: border-color .15s, background .15s; }
.experiment-task-card:hover, .experiment-task-card.selected { border-color: #7baa94; background: #f5faf7; }
.task-card-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; color: #2b3a34; font-size: 14px; }
.task-card-heading > b { overflow-wrap: anywhere; }
.experiment-task-card > small { display: block; margin-top: 7px; color: #687970; font-size: 12px; }
.task-status { display: inline-flex; flex: 0 0 auto; align-items: center; padding: 4px 7px; border: 1px solid #dce7e1; border-radius: 99px; background: #f5f8f6; color: #63736b; font-size: 11px; font-style: normal; white-space: nowrap; }
.task-status.running { border-color: #a8d3bc; background: #edf7f1; color: #16805c; }
.task-status.completed { border-color: #b7c9c0; color: #4d695b; }
.task-status.stopped { border-color: #ead4aa; background: #fff9ec; color: #88672b; }
.task-status.failed { border-color: #e8c2bd; background: #fff5f3; color: #b34a43; }
.task-card-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin-top: 13px; padding-top: 10px; border-top: 1px solid #e8eeeb; }
.task-card-metrics > span { display: grid; gap: 3px; min-width: 0; }
.task-card-metrics small { color: #74827c; font-size: 10px; }
.task-card-metrics b { overflow: hidden; color: #35463e; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.task-list-empty { padding: 18px 4px; color: #718078; font-size: 13px; line-height: 1.7; }
.experiment-detail-column { display: grid; gap: 16px; min-width: 0; }
.experiment-overview-panel .panel-heading { align-items: flex-start; }
.detail-heading-actions { display: flex; align-items: center; gap: 9px; }
.stop-task-button { height: 32px; padding: 0 10px; border: 1px solid #e7c6c1; border-radius: 4px; background: #fff; color: #b54b43; font-size: 12px; cursor: pointer; }
.stop-task-button:hover { background: #fff4f1; }
.stop-task-button:disabled { opacity: .6; cursor: wait; }
.task-caption { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 12px 0 14px; color: #718078; font-size: 12px; }
.task-error { margin: 0 0 12px; }
.task-overview-metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }
.task-overview-metrics .metric-cell { min-width: 0; padding: 11px 10px; border: 1px solid #e3ebe7; border-radius: 4px; background: #fbfcfb; }
.task-overview-metrics .metric-cell > span { color: #697970; font-size: 11px; }
.task-overview-metrics .metric-cell > b { display: block; overflow: hidden; margin-top: 8px; color: #2c3c35; font: 500 14px var(--mono); text-overflow: ellipsis; white-space: nowrap; }
.task-overview-metrics .metric-cell > small { display: block; margin-top: 5px; color: #74827b; font-size: 10px; }
.task-strategy-note { display: grid; gap: 5px; margin-top: 13px; padding: 10px 12px; border-left: 3px solid #5d9b7c; background: #f4f8f6; color: #56675f; font-size: 12px; line-height: 1.55; }
.task-strategy-note b { color: #33443b; }
.task-strategy-note small { color: #718078; font-size: 11px; }
.paper-holdings-panel, .paper-trades-panel { min-width: 0; }
.stock-open-button { display: grid; gap: 3px; padding: 2px 0; border: 0; background: transparent; color: #33443b; text-align: left; cursor: pointer; }
.stock-open-button:hover b { color: #14805c; text-decoration: underline; text-underline-offset: 3px; }
.stock-open-button b { font: 600 12px var(--mono); }
.stock-open-button small { color: #718078; font-size: 10px; }
.empty-task-detail { min-height: 215px; color: #718078; }
.empty-task-detail h3 { margin: 16px 0 7px; color: #2d3d36; font-size: 18px; }
.empty-task-detail p { max-width: 460px; font-size: 13px; line-height: 1.7; }
.paper-operation-note { padding: 16px 20px; }
.paper-operation-note p { margin: 8px 0 0; color: #5b6b64; font-size: 12px; line-height: 1.7; }
.paper-chart-panel { margin: 0; }
.paper-center .archive-table td small { display: block; margin-top: 4px; color: #708078; font-size: 11px; }

@media (max-width: 980px) {
  .experiment-layout { grid-template-columns: minmax(220px, 265px) minmax(0, 1fr); }
  .task-overview-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

@media (max-width: 720px) {
  .paper-page-heading { align-items: flex-start; }
  .experiment-layout { grid-template-columns: 1fr; }
  .experiment-task-panel { position: static; }
  .experiment-task-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .experiment-form-grid { grid-template-columns: 1fr; }
  .strategy-field, .symbol-field { grid-column: auto; grid-row: auto; }
}

@media (max-width: 500px) {
  .experiment-create-panel, .experiment-task-panel, .experiment-overview-panel, .empty-task-detail { padding: 15px 13px; }
  .experiment-task-list { grid-template-columns: 1fr; }
  .create-task-actions { align-items: stretch; flex-direction: column; }
  .create-task-actions .primary-button { width: 100%; }
  .task-overview-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .strategy-parameter-grid { grid-template-columns: 1fr; }
  .experiment-overview-panel .panel-heading { flex-direction: column; }
  .detail-heading-actions { width: 100%; justify-content: space-between; }
  .paper-page-heading { flex-direction: column; }
}
</style>
