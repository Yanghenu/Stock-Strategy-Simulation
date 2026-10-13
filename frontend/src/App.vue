<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api } from './api/client'
import BacktestChart from './components/BacktestChart.vue'
import FundExperimentCenter from './components/FundExperimentCenter.vue'
import FundMarket from './components/FundMarket.vue'
import IntradayChart from './components/IntradayChart.vue'
import MarketChart from './components/MarketChart.vue'
import PaperExperimentCenter from './components/PaperExperimentCenter.vue'
import QuoteCard from './components/QuoteCard.vue'
import StockSearch from './components/StockSearch.vue'
import TradeTable from './components/TradeTable.vue'
import { findExactStockName } from './utils/stockLookup'
import type { AppConfig, BacktestRecord, BacktestSummary, Bar, IntradayBar, PaperStrategy, Quote, WatchedStock } from './types'
import { aggregateIntradayOhlc, mergeLiveDailyBar } from './market-chart-utils'
import { intradayRefreshIntervalMs } from './utils/marketRefresh'

type ViewName = 'market' | 'backtest' | 'paper' | 'history' | 'fund-market' | 'fund-paper'
const view = ref<ViewName>('market')
const config = ref<AppConfig | null>(null)
const quote = ref<Quote | null>(null)
const marketSymbol = ref('600000')
const bars = ref<Bar[]>([])
const intradayBars = ref<IntradayBar[]>([])
const dailyIntradayBars = ref<IntradayBar[]>([])
const watchlist = ref<WatchedStock[]>([])
const watchlistLoading = ref(false)
const watchlistError = ref('')
const watchlistBusySymbol = ref('')
const loadingQuote = ref(false)
const loadingBars = ref(false)
const loadingIntraday = ref(false)
const loadingConfig = ref(true)
const errorMessage = ref('')
const marketUpdatedAt = ref('')
const intradayError = ref('')
const searchQuery = ref('')
const searchResults = ref<Quote[]>([])
const searching = ref(false)
const backtestStockName = ref('')
const backtestStockLookup = ref<'idle' | 'loading' | 'missing' | 'failed'>('idle')
const backtestResult = ref<BacktestRecord | null>(null)
const backtestHistory = ref<BacktestSummary[]>([])
const backtestStrategies = ref<PaperStrategy[]>([])
const strategyParameterValues = reactive<Record<string, Record<string, number>>>({})
const rotationSymbolsInput = ref('000333, 600000, 600028, 600276, 600887, 601899')
const activeRunId = ref('')
const submitting = ref(false)
const cancelling = ref(false)
const historyLoading = ref(false)
const backtestError = ref('')
let searchTimer: ReturnType<typeof setTimeout> | undefined
let searchSequence = 0
let backtestStockLookupTimer: ReturnType<typeof setTimeout> | undefined
let backtestStockLookupSequence = 0
let intradayRequestSequence = 0
let marketRequestSequence = 0
let intradaySymbol = ''
let pollTimer: ReturnType<typeof setTimeout> | undefined
let refreshTimer: ReturnType<typeof setInterval> | undefined
let chartRefreshTimer: ReturnType<typeof setInterval> | undefined
let intradayRefreshTimer: ReturnType<typeof setInterval> | undefined

function isoDate(value: Date): string {
  // 使用本地日期拼接 ISO 格式，避免浏览器 UTC 时区让日期少一天。
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

const today = isoDate(new Date())
const oneYearAgo = new Date()
oneYearAgo.setFullYear(oneYearAgo.getFullYear() - 1)
const form = reactive({
  symbol: '600000',
  strategy_code: 'dual_ma',
  start_date: isoDate(oneYearAgo),
  end_date: today,
  short_window: 5,
  long_window: 20,
  initial_capital: 100000,
  position_ratio: 1,
  commission_rate: 0.0003,
  minimum_commission: 5,
  stamp_duty_rate: 0.001,
  slippage_rate: 0.0005,
})

const isRunning = computed(() => ['queued', 'running'].includes(backtestResult.value?.status || ''))
const selectedStrategy = computed(() => backtestStrategies.value.find((item) => item.code === form.strategy_code))
const currentStrategyParameters = computed(() => strategyParameterValues[form.strategy_code] || {})
// 轮动使用多个股票代码；单股策略保留交易所前缀输入的旧行为。
const rotationSymbols = computed(() => rotationSymbolsInput.value.split(/[\s,，;；]+/).filter(Boolean))
const backtestValidationError = computed(() => {
  const strategy = selectedStrategy.value
  if (!strategy) return '正在读取策略目录'
  const symbols = form.strategy_code === 'momentum_rotation' ? rotationSymbols.value : [form.symbol]
  if (form.strategy_code === 'momentum_rotation' &&
      (!symbols.length || symbols.length > 20 || new Set(symbols).size !== symbols.length ||
       symbols.some((symbol) => !/^\d{6}$/.test(symbol)))) {
    return '请输入 1 至 20 只不重复的六位股票代码'
  }
  if (form.strategy_code !== 'momentum_rotation' && !/^(SH|SZ|BJ)?\d{6}$/i.test(form.symbol)) {
    return '请输入六位股票代码'
  }
  if (form.start_date > form.end_date) return '开始日期不能晚于结束日期'
  for (const parameter of strategy.parameters) {
    const value = currentStrategyParameters.value[parameter.key]
    const maximum = form.strategy_code === 'dual_ma'
      ? parameter.key === 'short_ma_period' ? 250 : 500 : parameter.max
    if (!Number.isFinite(value) || value < parameter.min || value > maximum ||
        (parameter.type === 'integer' && !Number.isInteger(value))) {
      return `${parameter.label}需在 ${parameter.min} 至 ${maximum} 之间`
    }
  }
  const values = currentStrategyParameters.value
  if (form.strategy_code === 'dual_ma' && values.short_ma_period >= values.long_ma_period) {
    return '短均线周期必须小于长均线周期'
  }
  return ''
})
function strategyName(code: string): string {
  return backtestStrategies.value.find((item) => item.code === code)?.name ||
    (code === 'dual_moving_average' ? '双均线交叉' : code)
}
const canCancel = computed(() => isRunning.value && !cancelling.value)
const backtestStockLabel = computed(() => {
  if (quote.value?.symbol === form.symbol) return quote.value.name
  if (backtestStockName.value) return backtestStockName.value
  if (backtestStockLookup.value === 'loading') return '查询中…'
  if (backtestStockLookup.value === 'missing') return '未查到名称'
  if (backtestStockLookup.value === 'failed') return '名称查询失败'
  return 'A 股代码'
})
const runLabel = computed(() => {
  const status = backtestResult.value?.status
  return ({ queued: '等待执行', running: '正在回测', completed: '回测完成', failed: '执行失败', cancelled: '已取消' } as Record<string, string>)[status || ''] || '待提交'
})
const liveDailyBars = computed(() => mergeLiveDailyBar(
  bars.value, dailyIntradayBars.value, quote.value, isoDate(new Date()),
))
const quoteWithIntradayOhlc = computed(() => {
  const current = quote.value
  // 只使用当前所选股票和当天分钟线，避免切换标的或交易日时短暂显示旧 OHLC。
  if (!current || current.symbol !== marketSymbol.value || intradaySymbol !== current.symbol) return current
  const ohlc = aggregateIntradayOhlc(intradayBars.value, isoDate(new Date()))
  return ohlc ? { ...current, ...ohlc, source: `${current.source} + 当日分时` } : current
})
const marketRangeLabel = computed(() => liveDailyBars.value.length
  ? `${liveDailyBars.value[0].trade_date} — ${liveDailyBars.value[liveDailyBars.value.length - 1].trade_date}`
  : '等待行情数据')
const isCurrentWatched = computed(() => !!quote.value && watchlist.value.some((item) => item.symbol === quote.value?.symbol))
const intradayLatestLabel = computed(() => {
  const latest = intradayBars.value.at(-1)?.trade_time
  return latest ? `最新分钟 ${latest.slice(11, 16)}` : '等待今日分时'
})
const intradayEmptyMessage = '今日暂无分钟数据，盘前、休市日或数据源尚未发布时会显示为空。'

async function loadQuote(symbol: string, keepError = false, forceRefresh = false, requestSequence = marketRequestSequence) {
  if (!keepError) errorMessage.value = ''
  loadingQuote.value = true
  try {
    const result = await api.getQuote(symbol, forceRefresh)
    if (requestSequence !== marketRequestSequence) return
    quote.value = result
    marketUpdatedAt.value = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch (error) {
    if (requestSequence === marketRequestSequence) {
      errorMessage.value = error instanceof Error ? error.message : '实时行情暂时不可用'
    }
  } finally {
    if (requestSequence === marketRequestSequence) loadingQuote.value = false
  }
}

async function loadBars(symbol: string, requestSequence = marketRequestSequence) {
  loadingBars.value = true
  const end = isoDate(new Date())
  const start = new Date()
  start.setFullYear(start.getFullYear() - 1)
  try {
    const result = await api.getBars(symbol, isoDate(start), end)
    if (requestSequence === marketRequestSequence) bars.value = result
  } catch (error) {
    if (requestSequence === marketRequestSequence) {
      bars.value = []
      errorMessage.value = error instanceof Error ? error.message : '日线数据暂时不可用'
    }
  } finally {
    if (requestSequence === marketRequestSequence) loadingBars.value = false
  }
}

async function loadIntradayBars(symbol: string) {
  // 每次切换标的递增序号，防止较慢的旧行情响应覆盖当前股票的分时图。
  const requestSequence = ++intradayRequestSequence
  if (intradaySymbol !== symbol) {
    intradaySymbol = symbol
    intradayBars.value = []
    dailyIntradayBars.value = []
    intradayError.value = ''
  }
  loadingIntraday.value = true
  try {
    const result = await api.getIntradayBars(symbol)
    if (requestSequence !== intradayRequestSequence) return
    intradayBars.value = result
    intradayError.value = ''
  } catch (error) {
    if (requestSequence === intradayRequestSequence) {
      // 刷新失败时保留上一批有效分钟线，并在图表标题处标明错误。
      intradayError.value = error instanceof Error ? error.message : '分时行情暂时不可用'
    }
  } finally {
    if (requestSequence === intradayRequestSequence) loadingIntraday.value = false
  }
}

async function refreshMarket(symbol = marketSymbol.value || form.symbol, forceRefresh = false) {
  // 切换股票时为整组请求发放序号，防止较慢的旧股票响应污染当前图表。
  const requestSequence = ++marketRequestSequence
  marketSymbol.value = symbol
  await Promise.all([
    loadQuote(symbol, false, forceRefresh, requestSequence),
    loadIntradayBars(symbol),
    loadBars(symbol, requestSequence),
  ])
  if (requestSequence === marketRequestSequence) dailyIntradayBars.value = intradayBars.value
}

async function chooseStock(item: Quote) {
  searchSequence += 1
  searchQuery.value = ''
  searchResults.value = []
  quote.value = item
  form.symbol = item.symbol
  view.value = 'market'
  await refreshMarket(item.symbol)
}

async function loadWatchlist() {
  watchlistLoading.value = true
  try {
    watchlist.value = await api.getWatchlist()
    watchlistError.value = ''
  } catch (error) {
    watchlistError.value = error instanceof Error ? error.message : '读取关注列表失败'
  } finally {
    watchlistLoading.value = false
  }
}

async function toggleCurrentWatch() {
  const current = quote.value
  if (!current || watchlistBusySymbol.value) return
  const symbol = current.symbol
  watchlistBusySymbol.value = symbol
  watchlistError.value = ''
  try {
    if (watchlist.value.some((item) => item.symbol === symbol)) {
      await api.removeWatchlist(symbol)
      watchlist.value = watchlist.value.filter((item) => item.symbol !== symbol)
    } else {
      const saved = await api.addWatchlist({ symbol, name: current.name })
      if (!watchlist.value.some((item) => item.symbol === symbol)) watchlist.value = [...watchlist.value, saved]
    }
  } catch (error) {
    watchlistError.value = error instanceof Error ? error.message : '保存关注列表失败'
  } finally {
    watchlistBusySymbol.value = ''
  }
}

async function removeWatchedStock(item: WatchedStock) {
  if (watchlistBusySymbol.value) return
  watchlistBusySymbol.value = item.symbol
  watchlistError.value = ''
  try {
    await api.removeWatchlist(item.symbol)
    watchlist.value = watchlist.value.filter((stock) => stock.symbol !== item.symbol)
  } catch (error) {
    watchlistError.value = error instanceof Error ? error.message : '取消关注失败'
  } finally {
    watchlistBusySymbol.value = ''
  }
}

function selectWatchedStock(item: WatchedStock) {
  form.symbol = item.symbol
  quote.value = null
  bars.value = []
  intradayBars.value = []
  intradaySymbol = ''
  void refreshMarket(item.symbol)
}

watch(searchQuery, (value) => {
  if (searchTimer) clearTimeout(searchTimer)
  const sequence = ++searchSequence
  if (value.trim().length < 2) {
    searchResults.value = []
    searching.value = false
    return
  }
  searchResults.value = []
  searchTimer = setTimeout(async () => {
    searching.value = true
    try {
      const results = await api.searchStocks(value.trim())
      if (sequence === searchSequence) searchResults.value = results
    } catch (error) {
      if (sequence === searchSequence) {
        errorMessage.value = error instanceof Error ? error.message : '股票搜索失败'
        searchResults.value = []
      }
    } finally {
      if (sequence === searchSequence) searching.value = false
    }
  }, 320)
})

watch(() => form.symbol, (value) => {
  // 代码变更时取消尚未发出的查询，并递增序号，防止旧响应覆盖新输入的名称。
  const symbol = value.trim()
  const sequence = ++backtestStockLookupSequence
  if (backtestStockLookupTimer) clearTimeout(backtestStockLookupTimer)
  backtestStockLookupTimer = undefined
  backtestStockName.value = ''
  backtestStockLookup.value = 'idle'
  if (!/^\d{6}$/.test(symbol)) return
  if (quote.value?.symbol === symbol) {
    backtestStockName.value = quote.value.name
    return
  }

  backtestStockLookup.value = 'loading'
  backtestStockLookupTimer = setTimeout(async () => {
    try {
      const results = await api.searchStocks(symbol)
      if (sequence !== backtestStockLookupSequence) return
      backtestStockName.value = findExactStockName(results, symbol)
      backtestStockLookup.value = backtestStockName.value ? 'idle' : 'missing'
    } catch {
      if (sequence === backtestStockLookupSequence) backtestStockLookup.value = 'failed'
    } finally {
      if (sequence === backtestStockLookupSequence) backtestStockLookupTimer = undefined
    }
  }, 320)
})

function setView(next: ViewName) {
  const returningToMarket = next === 'market' && view.value !== 'market'
  view.value = next
  if (next === 'history') void loadHistory()
  if (returningToMarket && quote.value) void refreshMarket(quote.value.symbol)
}

async function loadHistory() {
  historyLoading.value = true
  try {
    backtestHistory.value = (await api.listBacktests()).items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回测档案暂时不可用'
  } finally {
    historyLoading.value = false
  }
}

async function loadRun(runId: string) {
  backtestError.value = ''
  try {
    backtestResult.value = await api.getBacktest(runId)
    activeRunId.value = runId
    form.symbol = backtestResult.value.symbol
    form.strategy_code = backtestResult.value.strategy_code === 'dual_moving_average'
      ? 'dual_ma' : backtestResult.value.strategy_code
    rotationSymbolsInput.value = backtestResult.value.symbols.join(', ')
    form.start_date = backtestResult.value.start_date
    form.end_date = backtestResult.value.end_date
    form.short_window = backtestResult.value.short_window
    form.long_window = backtestResult.value.long_window
    form.initial_capital = backtestResult.value.initial_capital
    form.position_ratio = backtestResult.value.position_ratio
    form.commission_rate = backtestResult.value.commission_rate
    form.minimum_commission = backtestResult.value.minimum_commission
    form.stamp_duty_rate = backtestResult.value.stamp_duty_rate
    form.slippage_rate = backtestResult.value.slippage_rate
    // 回看档案时还原策略的独立列，用户可直接调整后再次提交。
    const record = backtestResult.value
    strategyParameterValues[form.strategy_code] = {
      short_ma_period: record.short_window, long_ma_period: record.long_window,
      ma_period: record.ma_period, breakout_period: record.breakout_period,
      atr_period: record.atr_period, atr_multiplier: record.atr_multiplier,
      momentum_period_days: record.momentum_period_days, trend_ma_period: record.trend_ma_period,
      max_positions: record.max_positions, rebalance_interval_days: record.rebalance_interval_days,
    }
    view.value = 'backtest'
    if (['queued', 'running'].includes(backtestResult.value.status)) schedulePoll(runId)
  } catch (error) {
    backtestError.value = error instanceof Error ? error.message : '读取回测详情失败'
  }
}

async function submitBacktest() {
  if (backtestValidationError.value) {
    backtestError.value = backtestValidationError.value
    return
  }
  backtestError.value = ''
  submitting.value = true
  try {
    const symbols = form.strategy_code === 'momentum_rotation' ? rotationSymbols.value : [form.symbol]
    const parameters = { ...currentStrategyParameters.value }
    const accepted = await api.submitBacktest({
      ...form, symbol: symbols[0], symbols, parameters,
    })
    activeRunId.value = accepted.run_id
    backtestResult.value = {
      run_id: accepted.run_id,
      symbol: symbols[0],
      symbols,
      start_date: form.start_date,
      end_date: form.end_date,
      strategy_code: form.strategy_code,
      symbol_count: symbols.length,
      short_window: parameters.short_ma_period ?? form.short_window,
      long_window: parameters.long_ma_period ?? form.long_window,
      ma_period: parameters.ma_period ?? 60,
      breakout_period: parameters.breakout_period ?? 20,
      atr_period: parameters.atr_period ?? 20,
      atr_multiplier: parameters.atr_multiplier ?? 2,
      momentum_period_days: parameters.momentum_period_days ?? 20,
      trend_ma_period: parameters.trend_ma_period ?? 60,
      max_positions: parameters.max_positions ?? 3,
      rebalance_interval_days: parameters.rebalance_interval_days ?? 7,
      status: accepted.status,
      progress: 0,
      total_return: null,
      annualized_return: null,
      max_drawdown: null,
      created_at: new Date().toISOString(),
      initial_capital: form.initial_capital,
      position_ratio: form.position_ratio,
      commission_rate: form.commission_rate,
      minimum_commission: form.minimum_commission,
      stamp_duty_rate: form.stamp_duty_rate,
      slippage_rate: form.slippage_rate,
      annualized_volatility: null,
      sharpe_ratio: null,
      win_rate: null,
      closed_trade_count: 0,
      fees_total: 0,
      dividends_total: 0,
      benchmark_return: null,
      equity_curve: [],
      trades: [],
      warnings: [],
    }
    schedulePoll(accepted.run_id)
    void loadHistory()
  } catch (error) {
    backtestError.value = error instanceof Error ? error.message : '无法提交回测任务'
  } finally {
    submitting.value = false
  }
}

function schedulePoll(runId: string) {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = setTimeout(() => void pollRun(runId), 1100)
}

async function pollRun(runId: string) {
  try {
    const record = await api.getBacktest(runId)
    if (activeRunId.value !== runId) return
    backtestResult.value = record
    if (['queued', 'running'].includes(record.status)) schedulePoll(runId)
    else void loadHistory()
  } catch (error) {
    backtestError.value = error instanceof Error ? error.message : '读取回测进度失败'
  }
}

async function cancelRun() {
  if (!activeRunId.value) return
  cancelling.value = true
  try {
    await api.cancelBacktest(activeRunId.value)
    schedulePoll(activeRunId.value)
  } catch (error) {
    backtestError.value = error instanceof Error ? error.message : '取消请求失败'
  } finally {
    cancelling.value = false
  }
}

function percent(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(2)}%`
}
function amount(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : `¥${value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`
}
onMounted(async () => {
  try {
    config.value = await api.getConfig()
    Object.assign(form, config.value.defaults)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取应用配置失败'
  } finally {
    loadingConfig.value = false
  }
  try {
    backtestStrategies.value = await api.getBacktestStrategies()
    // 每个策略单独保留表单值，切换策略时无需重新输入参数。
    for (const strategy of backtestStrategies.value) {
      strategyParameterValues[strategy.code] = Object.fromEntries(
        strategy.parameters.map((parameter) => [parameter.key, parameter.default]),
      )
    }
    if (config.value) {
      strategyParameterValues.dual_ma.short_ma_period = config.value.defaults.short_window
      strategyParameterValues.dual_ma.long_ma_period = config.value.defaults.long_window
    }
  } catch (error) {
    backtestError.value = error instanceof Error ? error.message : '读取策略目录失败'
  }
  void loadWatchlist()
  await refreshMarket(form.symbol)
  void loadHistory()
  const quoteInterval = Math.max(20, config.value?.refresh_interval_seconds ?? 60) * 1000
  const chartInterval = Math.max(5, config.value?.chart_refresh_interval_seconds ?? 10) * 1000
  const intradayInterval = intradayRefreshIntervalMs(config.value?.intraday_refresh_interval_seconds)
  refreshTimer = setInterval(() => {
    // 全市场快照仍按原频率刷新，避免提高后台和纸面实验的行情源压力。
    if (document.hidden) return
    if (view.value === 'market') {
      void loadQuote(marketSymbol.value, true)
    }
  }, quoteInterval)
  chartRefreshTimer = setInterval(() => {
    // 日线图只读取已拿到的分时快照，按原配置更新当日 K 线。
    if (document.hidden || view.value !== 'market') return
    dailyIntradayBars.value = intradayBars.value
  }, chartInterval)
  intradayRefreshTimer = setInterval(() => {
    // 分时独立按配置轮询；接口仍按上游分钟线节奏提供新数据。
    if (document.hidden || view.value !== 'market' || loadingIntraday.value) return
    void loadIntradayBars(marketSymbol.value)
  }, intradayInterval)
})

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  // 卸载时清理未发送的股票名称查询，并让已发出的旧响应失效。
  if (backtestStockLookupTimer) clearTimeout(backtestStockLookupTimer)
  backtestStockLookupSequence += 1
  if (pollTimer) clearTimeout(pollTimer)
  if (refreshTimer) clearInterval(refreshTimer)
  if (chartRefreshTimer) clearInterval(chartRefreshTimer)
  if (intradayRefreshTimer) clearInterval(intradayRefreshTimer)
})
</script>

<template>
  <div class="app-shell">
    <!-- 顶栏保持股票、基金行情与实验入口常驻，窄屏时允许导航横向滚动。 -->
    <header class="topbar">
      <a class="brand" href="#top" @click.prevent="setView('market')">
        <span class="brand-mark"><i></i><i></i><i></i><i></i></span>
        <span class="brand-copy"><b>QUANT DESK</b><small>个人量化研究终端</small></span>
      </a>
      <nav class="main-nav" aria-label="主导航">
        <button :class="{ active: view === 'market' }" @click="setView('market')"><span>01</span> 行情监控</button>
        <button :class="{ active: view === 'backtest' }" @click="setView('backtest')"><span>02</span> 策略回测</button>
        <button :class="{ active: view === 'paper' }" @click="setView('paper')"><span>03</span> 纸面实验</button>
        <button :class="{ active: view === 'history' }" @click="setView('history')"><span>04</span> 回测档案</button>
        <button :class="{ active: view === 'fund-market' }" @click="setView('fund-market')"><span>05</span> 基金行情</button>
        <button :class="{ active: view === 'fund-paper' }" @click="setView('fund-paper')"><span>06</span> 基金实验</button>
      </nav>
      <div class="topbar-status">
        <span class="live-dot"></span>
        <span>{{ loadingConfig ? '连接中' : '本机工作区' }}</span>
        <span class="status-divider"></span>
        <span class="status-clock">{{ marketUpdatedAt || '行情待更新' }}</span>
      </div>
    </header>

    <main id="top" class="page-content">
      <div v-if="errorMessage" class="notice-banner" role="status">
        <span class="notice-icon">!</span><span>{{ errorMessage }}</span>
        <button aria-label="关闭提示" @click="errorMessage = ''">×</button>
      </div>

      <template v-if="view === 'market'">
        <!-- 行情页先看实时快照与当日分时，再查看较长区间的日线。 -->
        <section class="page-heading market-heading">
          <div>
            <div class="eyebrow"><span class="section-index">01 / MARKET</span> 市场概览</div>
            <p>盘中分时走势与前复权日线，查看你关注的每一笔波动。</p>
          </div>
          <div class="heading-tools">
            <StockSearch v-model="searchQuery" :results="searchResults" :searching="searching" @select="chooseStock" />
            <button class="icon-button refresh-button" title="刷新行情、分时与日线" :disabled="loadingQuote || loadingIntraday || loadingBars" @click="refreshMarket(undefined, true)">
              <span :class="{ rotating: loadingQuote || loadingIntraday || loadingBars }">↻</span><span class="refresh-label">刷新</span>
            </button>
          </div>
        </section>

        <QuoteCard
          :quote="quoteWithIntradayOhlc"
          :loading="loadingQuote"
          :watched="isCurrentWatched"
          :watch-pending="watchlistBusySymbol === quote?.symbol || watchlistLoading"
          @toggle-watch="toggleCurrentWatch"
        />

        <section class="panel chart-panel intraday-panel">
          <div class="panel-heading">
            <div><span class="eyebrow">TODAY · 1 MINUTE</span><h3>当日分时行情</h3></div>
            <div class="chart-range"><span class="range-dot"></span>{{ intradayLatestLabel }}<span v-if="intradayError" class="intraday-warning">{{ intradayError }}</span></div>
          </div>
          <IntradayChart
            :bars="intradayBars"
            :loading="loadingIntraday"
            :error="intradayError"
            :empty-message="intradayEmptyMessage"
            :previous-close="quote?.previous_close ?? 0"
          />
          <div class="chart-footnote"><span>分时价格 · 成交量（股）</span><span>AKShare · 东方财富（新浪备用）· 每 {{ config?.intraday_refresh_interval_seconds ?? 2 }} 秒轮询</span></div>
        </section>

        <section class="panel chart-panel">
          <div class="panel-heading">
            <div><span class="eyebrow">PRICE ACTION</span><h3>日线走势</h3></div>
            <div class="chart-range"><span class="range-dot"></span>{{ marketRangeLabel }}<span class="range-separator"></span>前复权</div>
          </div>
          <MarketChart :bars="liveDailyBars" :loading="loadingBars" />
          <div class="chart-footnote"><span>价格与成交量</span><span>历史日线缓存 · 当日 K 线每 {{ config?.chart_refresh_interval_seconds ?? 10 }} 秒更新</span></div>
        </section>

        <section class="market-lower-grid">
          <div class="panel compact-panel">
            <div class="panel-heading"><div><span class="eyebrow">WATCHLIST</span><h3>关注标的</h3></div><span class="count-pill">{{ String(watchlist.length).padStart(2, '0') }}</span></div>
            <div v-if="watchlistLoading && !watchlist.length" class="small-empty">正在读取关注列表…</div>
            <div v-for="item in watchlist" :key="item.symbol" class="watch-row" :class="{ 'is-selected': quote?.symbol === item.symbol }">
              <button class="watch-select" :aria-label="`查看${item.name}行情`" @click="selectWatchedStock(item)">
                <span class="watch-symbol">{{ item.symbol }}</span><span class="watch-name">{{ item.name }}</span>
                <b v-if="quote?.symbol === item.symbol" :class="quote.change_percent >= 0 ? 'up' : 'down'">{{ quote.change_percent >= 0 ? '+' : '' }}{{ quote.change_percent.toFixed(2) }}%</b>
                <span v-else class="watch-open-label">查看</span>
              </button>
              <button class="watch-remove" :disabled="watchlistBusySymbol === item.symbol" :aria-label="`移除${item.name}关注`" @click.stop="removeWatchedStock(item)">×</button>
            </div>
            <div v-if="!watchlistLoading && !watchlist.length" class="small-empty">查看股票后，点击行情卡上的「关注」保存到 SQL Server</div>
            <p v-if="watchlistError" class="watchlist-error" role="status">{{ watchlistError }}</p>
            <div class="panel-note">SQL Server · 多浏览器共享</div>
          </div>
          <div class="panel compact-panel market-note-panel">
            <span class="eyebrow">DATA NOTE</span>
            <h3>行情刷新节奏</h3>
            <p>分时每 {{ config?.intraday_refresh_interval_seconds ?? 2 }} 秒轮询，当日 K 线每 {{ config?.chart_refresh_interval_seconds ?? 10 }} 秒更新；全市场快照和纸面实验沿用 {{ config?.refresh_interval_seconds ?? 60 }} 秒设置。</p>
            <div class="note-line"><span>当前数据区间</span><b>{{ liveDailyBars.length }} 个交易日</b></div>
          </div>
        </section>
      </template>

      <template v-else-if="view === 'backtest'">
        <!-- 回测页左侧只编辑参数，右侧展示异步任务结果与成交账本。 -->
        <section class="page-heading backtest-heading">
          <div>
            <div class="eyebrow"><span class="section-index">02 / RESEARCH</span> 历史回测</div>
            <p>{{ selectedStrategy?.name || '策略回测' }} · 收盘生成信号 · 下一交易日开盘执行</p>
          </div>
          <div class="strategy-stamp"><span>STRATEGY</span><b>{{ selectedStrategy?.name || 'STRATEGY' }}</b><small>LONG ONLY · T+1</small></div>
        </section>

        <div class="backtest-layout">
          <section class="panel form-panel">
            <div class="panel-heading form-heading"><div><span class="eyebrow">CONFIGURATION</span><h3>回测参数</h3></div><span class="required-mark">所有字段均在本机处理</span></div>
            <form class="backtest-form" @submit.prevent="submitBacktest">
              <div class="form-section-label"><span>01</span> 标的与区间</div>
              <label v-if="form.strategy_code !== 'momentum_rotation'" class="form-field"><span>股票代码</span><div class="input-combo"><input v-model.trim="form.symbol" maxlength="8" required /><span>{{ backtestStockLabel }}</span></div></label>
              <label v-else class="form-field"><span>组合股票代码（逗号或空格分隔，最多 20 只）</span><textarea v-model="rotationSymbolsInput" class="backtest-symbols-input" rows="3" required></textarea></label>
              <div class="form-row">
                <label class="form-field"><span>开始日期</span><input v-model="form.start_date" type="date" required /></label>
                <label class="form-field"><span>结束日期</span><input v-model="form.end_date" type="date" required /></label>
              </div>

              <div class="form-section-label"><span>02</span> 策略参数</div>
              <label class="form-field"><span>使用策略</span><select v-model="form.strategy_code" class="backtest-strategy-select"><option v-for="strategy in backtestStrategies" :key="strategy.code" :value="strategy.code">{{ strategy.name }}</option></select></label>
              <p v-if="selectedStrategy" class="backtest-strategy-description">{{ selectedStrategy.description }}</p>
              <div v-if="selectedStrategy" class="backtest-strategy-grid">
                <label v-for="parameter in selectedStrategy.parameters" :key="parameter.key" class="form-field">
                  <span>{{ parameter.label }}</span>
                  <div class="number-field"><input v-model.number="strategyParameterValues[form.strategy_code][parameter.key]" type="number" :min="parameter.min" :max="form.strategy_code === 'dual_ma' ? parameter.key === 'short_ma_period' ? 250 : 500 : parameter.max" :step="parameter.step" required /><small>{{ parameter.unit }}</small></div>
                </label>
              </div>
              <div class="form-row">
                <label class="form-field"><span>初始资金</span><div class="number-field prefix"><small>¥</small><input v-model.number="form.initial_capital" type="number" min="1000" step="1000" required /></div></label>
                <label class="form-field"><span>仓位比例</span><div class="number-field"><input v-model.number="form.position_ratio" type="number" min="0.01" max="1" step="0.05" required /><small>比例</small></div></label>
              </div>

              <details class="fee-details">
                <summary><span>费用与滑点</span><span>展开参数 <b>＋</b></span></summary>
                <div class="form-row fee-row">
                  <label class="form-field"><span>佣金费率</span><input v-model.number="form.commission_rate" type="number" min="0" step="0.0001" /></label>
                  <label class="form-field"><span>最低佣金</span><input v-model.number="form.minimum_commission" type="number" min="0" step="1" /></label>
                </div>
                <div class="form-row fee-row">
                  <label class="form-field"><span>卖出印花税</span><input v-model.number="form.stamp_duty_rate" type="number" min="0" step="0.0001" /></label>
                  <label class="form-field"><span>滑点比例</span><input v-model.number="form.slippage_rate" type="number" min="0" max="0.99" step="0.0001" /></label>
                </div>
              </details>

              <div v-if="backtestError || backtestValidationError" class="form-error">{{ backtestError || backtestValidationError }}</div>
              <button class="primary-button submit-button" type="submit" :disabled="submitting || isRunning || Boolean(backtestValidationError)">
                <span v-if="submitting || isRunning" class="spinner small"></span>
                {{ submitting ? '正在提交…' : isRunning ? '回测正在运行' : '运行历史回测' }}
                <span v-if="!submitting && !isRunning">↗</span>
              </button>
              <button v-if="canCancel" class="text-button cancel-button" type="button" @click="cancelRun">{{ cancelling ? '正在请求取消…' : '取消当前回测' }}</button>
            </form>
          </section>

          <section class="result-column">
            <div class="panel result-panel">
              <div class="panel-heading result-header">
                <div><span class="eyebrow">PERFORMANCE</span><h3>策略表现</h3></div>
                <span :class="['run-status', (backtestResult?.status || 'idle')]"><i></i>{{ runLabel }}</span>
              </div>
              <div v-if="backtestResult" class="run-caption">
                <span>{{ backtestResult.symbols.join('、') }} · {{ strategyName(backtestResult.strategy_code) }}</span>
                <span v-if="isRunning">{{ backtestResult.progress || 0 }}% 完成</span>
                <span v-else>{{ backtestResult.start_date }} 至 {{ backtestResult.end_date }}</span>
              </div>
              <div v-if="backtestResult?.status === 'failed' && backtestResult.error_message" class="form-error result-error">{{ backtestResult.error_message }}</div>
              <div class="metrics-grid">
                <div class="metric-cell primary-metric"><span>策略收益</span><b :class="(backtestResult?.total_return || 0) >= 0 ? 'up' : 'down'">{{ percent(backtestResult?.total_return) }}</b><small>区间累计</small></div>
                <div class="metric-cell"><span>年化收益</span><b>{{ percent(backtestResult?.annualized_return) }}</b><small>按交易日折算</small></div>
                <div class="metric-cell"><span>最大回撤</span><b class="down">{{ percent(backtestResult?.max_drawdown) }}</b><small>峰值至谷值</small></div>
                <div class="metric-cell"><span>夏普比率</span><b>{{ backtestResult?.sharpe_ratio?.toFixed(2) ?? '—' }}</b><small>日收益年化</small></div>
                <div class="metric-cell"><span>胜率</span><b>{{ percent(backtestResult?.win_rate) }}</b><small>{{ backtestResult?.closed_trade_count ?? 0 }} 笔已平仓</small></div>
                <div class="metric-cell"><span>基准收益</span><b>{{ percent(backtestResult?.benchmark_return) }}</b><small>{{ backtestResult?.strategy_code === 'momentum_rotation' ? '股票池等权买入持有' : '买入持有' }}</small></div>
                <div class="metric-cell"><span>年化波动</span><b>{{ percent(backtestResult?.annualized_volatility) }}</b><small>日收益年化</small></div>
                <div class="metric-cell"><span>累计费用</span><b>{{ amount(backtestResult?.fees_total) }}</b><small>佣金与印花税</small></div>
              </div>
              <div v-if="isRunning" class="progress-track"><i :style="{ width: `${Math.max(3, backtestResult?.progress || 0)}%` }"></i></div>
            </div>

            <div class="panel chart-panel backtest-graph-panel">
              <div class="panel-heading"><div><span class="eyebrow">EQUITY CURVE</span><h3>净值与回撤</h3></div><span class="chart-unit">净值 ¥ · 回撤 %</span></div>
              <BacktestChart :result="backtestResult" />
            </div>

            <div class="panel trades-panel">
              <div class="panel-heading"><div><span class="eyebrow">EXECUTIONS</span><h3>成交记录</h3></div><span class="count-pill">{{ backtestResult?.trades.length ?? 0 }}</span></div>
              <TradeTable :trades="backtestResult?.trades ?? []" />
            </div>
            <div v-if="backtestResult?.warnings.length" class="warning-list">
              <span class="eyebrow">执行说明</span>
              <p v-for="(warning, index) in backtestResult.warnings" :key="index">{{ warning }}</p>
            </div>
          </section>
        </div>
      </template>

      <template v-else-if="view === 'paper'">
        <PaperExperimentCenter
          :refresh-interval-seconds="config?.refresh_interval_seconds ?? 60"
          :initial-capital="config?.defaults.initial_capital ?? 100000"
        />
      </template>

      <template v-else-if="view === 'fund-market'">
        <FundMarket
          :exchange-refresh-interval-seconds="config?.fund.exchange_refresh_interval_seconds ?? 10"
          :open-nav-refresh-interval-seconds="config?.fund.open_nav_refresh_interval_seconds ?? 300"
          @error="errorMessage = $event"
        />
      </template>

      <template v-else-if="view === 'fund-paper'">
        <FundExperimentCenter
          :refresh-interval-seconds="config?.fund.worker_interval_seconds ?? 60"
          :initial-capital="config?.fund.default_initial_capital ?? 100000"
        />
      </template>

      <template v-else>
        <!-- 档案页从 SQL Server 分页读取已保存回测，并在打开时加载完整曲线。 -->
        <section class="page-heading history-heading">
          <div>
            <div class="eyebrow"><span class="section-index">03 / ARCHIVE</span> 研究记录</div>
            <p>回测结果与成交账本保存在配置的 SQL Server 中。</p>
          </div>
          <button class="secondary-button" @click="loadHistory" :disabled="historyLoading"><span :class="{ rotating: historyLoading }">↻</span> 刷新档案</button>
        </section>
        <section class="panel archive-panel">
          <div class="panel-heading"><div><span class="eyebrow">BACKTEST RUNS</span><h3>回测档案</h3></div><span class="count-pill">{{ backtestHistory.length.toString().padStart(2, '0') }}</span></div>
          <div class="archive-table-scroll">
            <table class="archive-table">
              <thead><tr><th>标的</th><th>策略</th><th>回测区间</th><th>累计收益</th><th>最大回撤</th><th>状态</th><th></th></tr></thead>
              <tbody>
                <tr v-for="item in backtestHistory" :key="item.run_id" @click="loadRun(item.run_id)">
                  <td><b>{{ item.symbol }}</b><small>{{ item.symbol_count && item.symbol_count > 1 ? `等 ${item.symbol_count} 只股票` : item.strategy_code === 'dual_ma' || item.strategy_code === 'dual_moving_average' ? `MA${item.short_window} / MA${item.long_window}` : '单股' }}</small></td>
                  <td class="muted">{{ strategyName(item.strategy_code) }}</td>
                  <td>{{ item.start_date }} — {{ item.end_date }}</td>
                  <td :class="(item.total_return || 0) >= 0 ? 'up' : 'down'">{{ percent(item.total_return) }}</td>
                  <td class="down">{{ percent(item.max_drawdown) }}</td>
                  <td><span :class="['archive-status', item.status]">{{ item.status === 'completed' ? '完成' : item.status === 'running' ? '运行中' : item.status === 'queued' ? '排队中' : item.status === 'failed' ? '失败' : '取消' }}</span></td>
                  <td><button class="open-run" aria-label="查看回测详情">↗</button></td>
                </tr>
                <tr v-if="!backtestHistory.length"><td colspan="7" class="table-empty">{{ historyLoading ? '正在读取档案…' : '还没有回测记录，提交第一次历史回测后会显示在这里。' }}</td></tr>
              </tbody>
            </table>
          </div>
          <div class="archive-foot"><span>最新回测优先 · 每页最多显示 50 条</span><span>本地数据库</span></div>
        </section>
      </template>

      <footer class="app-footer">
        <span><b>QD</b> Quant Desk <i>·</i> 股票与基金量化研究</span>
        <span>行情仅供研究 · 模拟成交不代表实际交易表现</span>
      </footer>
    </main>
  </div>
</template>
