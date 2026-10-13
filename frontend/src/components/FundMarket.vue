<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api/client'
import FundNavChart from './FundNavChart.vue'
import IntradayChart from './IntradayChart.vue'
import MarketChart from './MarketChart.vue'
import type { FundBar, FundInstrument, FundIntradayBar, FundNavPoint, FundQuote, FundType } from '../types'
import { formatFundChange, formatFundNav, formatFundNavDate, fundTypeLabel } from '../utils/fundFormat'
import { fundSearchTerm } from '../utils/fundSearch'

const props = defineProps<{ exchangeRefreshIntervalSeconds: number; openNavRefreshIntervalSeconds: number }>()
const emit = defineEmits<{ select: [fund: FundInstrument]; error: [message: string] }>()
const fundType = ref<FundType>('etf')
const query = ref('')
const instruments = ref<FundInstrument[]>([])
const selected = ref<FundInstrument | null>(null)
const quote = ref<FundQuote | null>(null)
const bars = ref<FundBar[]>([])
const intraday = ref<FundIntradayBar[]>([])
const nav = ref<FundNavPoint[]>([])
const searching = ref(false)
const loadingQuote = ref(false)
const loadingBars = ref(false)
const loadingIntraday = ref(false)
const loadingNav = ref(false)
const errorMessage = ref('')
const searchError = ref('')
const intradayError = ref('')
const isExchange = computed(() => fundType.value !== 'open_end')
const latestNav = computed(() => nav.value.at(-1) || null)
const refreshSeconds = computed(() => fundType.value === 'open_end'
  ? props.openNavRefreshIntervalSeconds : props.exchangeRefreshIntervalSeconds)
let searchTimer: ReturnType<typeof setTimeout> | undefined
let quoteTimer: ReturnType<typeof setInterval> | undefined
let chartTimer: ReturnType<typeof setInterval> | undefined
let requestId = 0
let searchId = 0

function clearMarketData() {
  quote.value = null
  bars.value = []
  intraday.value = []
  nav.value = []
  errorMessage.value = ''
  intradayError.value = ''
}

async function loadInstruments() {
  const current = ++searchId
  const term = fundSearchTerm(query.value)
  if (!term) {
    instruments.value = []
    searching.value = false
    searchError.value = ''
    return
  }
  searching.value = true
  searchError.value = ''
  try {
    const result = await api.searchFunds(term, fundType.value)
    if (current !== searchId) return
    instruments.value = result
    // 仅代码或名称完全匹配时自动打开；模糊搜索保留候选列表供用户挑选。
    const exactMatch = result.find((fund) => fund.symbol === term || fund.name === term)
    if (exactMatch && (selected.value?.symbol !== exactMatch.symbol || selected.value.fund_type !== exactMatch.fund_type)) {
      await chooseFund(exactMatch)
    }
  } catch (error) {
    if (current === searchId) {
      searchError.value = error instanceof Error ? error.message : '基金搜索暂时不可用'
      emit('error', searchError.value)
    }
  } finally {
    if (current === searchId) searching.value = false
  }
}

async function loadSelected(forceRefresh: boolean, includeHistory = true) {
  const fund = selected.value
  if (!fund) return
  const current = ++requestId
  loadingQuote.value = true
  errorMessage.value = ''
  try {
    const quotePromise = api.getFundQuote(fund.fund_type, fund.symbol, forceRefresh)
    if (fund.fund_type === 'open_end') {
      loadingNav.value = true
      const [quoteResult, navResult] = await Promise.allSettled([
        quotePromise,
        api.getFundNav(fund.fund_type, fund.symbol),
      ])
      if (current !== requestId) return
      if (quoteResult.status === 'fulfilled') quote.value = quoteResult.value
      else errorMessage.value = quoteResult.reason instanceof Error ? quoteResult.reason.message : '基金净值暂不可用'
      if (navResult.status === 'fulfilled') nav.value = navResult.value
      else if (!errorMessage.value) errorMessage.value = navResult.reason instanceof Error ? navResult.reason.message : '基金净值曲线暂不可用'
    } else {
      if (!includeHistory) {
        const nextQuote = await quotePromise
        if (current === requestId) {
          quote.value = nextQuote
          emit('error', '')
        }
        return
      }
      loadingIntraday.value = true
      const end = new Date()
      const start = new Date()
      start.setFullYear(start.getFullYear() - 1)
      const [quoteResult, intradayResult, barsResult] = await Promise.allSettled([
        quotePromise,
        api.getFundIntraday(fund.fund_type, fund.symbol),
        api.getFundBars(fund.fund_type, fund.symbol, dateKey(start), dateKey(end)),
      ])
      if (current !== requestId) return
      if (quoteResult.status === 'fulfilled') quote.value = quoteResult.value
      else errorMessage.value = quoteResult.reason instanceof Error ? quoteResult.reason.message : '基金报价暂不可用'
      if (intradayResult.status === 'fulfilled') {
        intraday.value = intradayResult.value
        intradayError.value = ''
      } else {
        intradayError.value = intradayResult.reason instanceof Error ? intradayResult.reason.message : '分时行情暂不可用'
      }
      if (barsResult.status === 'fulfilled') bars.value = barsResult.value
      else if (!errorMessage.value) errorMessage.value = barsResult.reason instanceof Error ? barsResult.reason.message : '基金日线暂不可用'
    }
  } catch (error) {
    if (current === requestId) {
      // 刷新失败时保留上一批有效行情，提示中注明缓存行情状态。
      errorMessage.value = error instanceof Error ? error.message : '基金行情暂时不可用'
      emit('error', errorMessage.value)
    }
  } finally {
    if (current === requestId) {
      loadingQuote.value = false
      loadingBars.value = false
      loadingIntraday.value = false
      loadingNav.value = false
    }
  }
  emit('error', errorMessage.value)
}

function dateKey(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
}

async function chooseFund(fund: FundInstrument) {
  requestId += 1
  selected.value = fund
  clearMarketData()
  loadingBars.value = fund.fund_type !== 'open_end'
  loadingNav.value = fund.fund_type === 'open_end'
  emit('select', fund)
  await loadSelected(true)
}

async function refreshAll() {
  if (!selected.value) return
  loadingBars.value = isExchange.value
  await loadSelected(true)
}

watch(fundType, async () => {
  // 类型切换时立即清空旧标的数据，避免同代码 ETF 与开放基金混画。
  selected.value = null
  instruments.value = []
  clearMarketData()
  await loadInstruments()
})
watch(query, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { void loadInstruments() }, 320)
})

onMounted(() => { void loadInstruments() })
watch([() => selected.value?.symbol, () => selected.value?.fund_type, refreshSeconds], () => {
  if (quoteTimer) clearInterval(quoteTimer)
  if (chartTimer) clearInterval(chartTimer)
  if (!selected.value) return
  // 报价遵循配置刷新，日线不高频下载，分钟图每 30 秒检查一次。
  quoteTimer = setInterval(() => { if (!document.hidden) void loadSelected(false, false) }, Math.max(5, refreshSeconds.value) * 1000)
  if (selected.value.fund_type !== 'open_end') {
    chartTimer = setInterval(() => {
      if (document.hidden || loadingIntraday.value || !selected.value) return
      void api.getFundIntraday(selected.value.fund_type, selected.value.symbol).then((result) => { intraday.value = result; intradayError.value = '' })
        .catch((error: unknown) => { intradayError.value = error instanceof Error ? error.message : '分时行情暂不可用' })
    }, Math.max(30, refreshSeconds.value * 3) * 1000)
  }
}, { immediate: true })

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  if (quoteTimer) clearInterval(quoteTimer)
  if (chartTimer) clearInterval(chartTimer)
  requestId += 1
  searchId += 1
})
</script>

<template>
  <section class="page-heading fund-page-heading">
    <div>
      <div class="eyebrow"><span class="section-index">05 / FUND MARKET</span> 基金行情</div>
      <p>ETF、LOF 查看盘中交易行情；开放式基金展示已公布单位净值。</p>
    </div>
    <div class="heading-tools fund-market-tools">
      <label class="fund-type-select"><span>基金类型</span><select v-model="fundType"><option value="etf">ETF</option><option value="lof">LOF</option><option value="open_end">开放式基金</option></select></label>
      <label class="fund-search"><span>代码或名称</span><input v-model.trim="query" placeholder="例如 510300 / 华夏成长" @keyup.enter="loadInstruments"></label>
      <button class="secondary-button" :disabled="searching" @click="refreshAll"><span :class="{ rotating: loadingQuote }">↻</span> 刷新</button>
    </div>
  </section>

  <div class="fund-market-layout">
    <aside class="panel fund-list-panel">
      <div class="panel-heading"><div><span class="eyebrow">FUND UNIVERSE</span><h3>{{ fundTypeLabel(fundType) }}列表</h3></div><span class="count-pill">{{ instruments.length }}</span></div>
      <div v-if="searching && !instruments.length" class="small-empty">正在读取基金列表…</div>
      <div v-for="fund in instruments" :key="`${fund.fund_type}-${fund.symbol}`" class="fund-list-row" :class="{ selected: selected?.symbol === fund.symbol }">
        <button @click="chooseFund(fund)"><b>{{ fund.name }}</b><span>{{ fund.symbol }} <small>{{ fund.category }}</small></span></button>
      </div>
      <div v-if="!searching && !instruments.length" class="small-empty">{{ searchError || (query ? '没有匹配的基金，请调整关键词。' : '输入基金代码或名称后搜索。') }}</div>
      <div class="panel-note">列表与基金类型来自 AKShare；已查询数据保存在 SQL Server</div>
    </aside>

    <div class="fund-market-content">
      <section v-if="quote" class="panel fund-quote-panel">
        <div class="fund-quote-top">
          <div><span class="eyebrow">{{ fundTypeLabel(quote.fund_type) }} · {{ quote.symbol }}</span><h2>{{ quote.name }}</h2><p>{{ quote.category || '基金' }} · {{ quote.stale ? '缓存快照' : quote.source }}</p></div>
          <span :class="['freshness', quote.stale ? 'is-stale' : '']"><i></i>{{ quote.stale ? '行情缓存' : '数据已同步' }}</span>
        </div>
        <div class="fund-quote-metrics">
          <div class="fund-price-cell"><span>{{ quote.fund_type === 'open_end' ? '最新公布单位净值' : '最新场内价格' }}</span><b :class="quote.change_percent >= 0 ? 'up' : 'down'">{{ formatFundNav(quote.latest_price) }}</b><small>{{ formatFundChange(quote.change_percent) }} <template v-if="quote.fund_type === 'open_end'">· 净值日期 {{ formatFundNavDate(quote.nav_date) }}</template></small></div>
          <div v-if="quote.fund_type === 'open_end'" class="fund-info-cell"><span>累计净值</span><b>{{ formatFundNav(quote.accumulated_nav) }}</b><small>申购 {{ quote.subscription_status }} · 赎回 {{ quote.redemption_status }}</small></div>
          <div v-else class="fund-info-cell"><span>今开 / 昨收</span><b>{{ formatFundNav(quote.open) }} / {{ formatFundNav(quote.previous_close) }}</b><small>最高 {{ formatFundNav(quote.high) }} · 最低 {{ formatFundNav(quote.low) }}</small></div>
          <div class="fund-info-cell"><span>{{ quote.fund_type === 'open_end' ? '费率说明' : '成交额 / 成交量' }}</span><b>{{ quote.fund_type === 'open_end' ? quote.fee_description || '以基金公告为准' : `¥${quote.turnover.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}` }}</b><small>{{ quote.fund_type === 'open_end' ? `数据更新于 ${new Date(quote.fetched_at).toLocaleTimeString('zh-CN')}` : `成交量 ${quote.volume.toLocaleString('zh-CN', { maximumFractionDigits: 0 })} 股 · 更新于 ${new Date(quote.fetched_at).toLocaleTimeString('zh-CN')}` }}</small></div>
        </div>
        <p v-if="errorMessage" class="form-error fund-inline-error">{{ errorMessage }}</p>
      </section>
      <section v-else class="panel fund-quote-empty"><span class="eyebrow">MARKET SNAPSHOT</span><p>{{ selected ? '正在读取所选基金行情…' : instruments.length ? '从左侧列表选择基金查看行情' : '搜索基金代码或名称，再选择基金查看行情。' }}</p></section>

      <template v-if="selected?.fund_type === 'open_end'">
        <section class="panel chart-panel fund-chart-panel">
          <div class="panel-heading"><div><span class="eyebrow">PUBLISHED NAV</span><h3>单位净值与累计净值</h3></div><span class="chart-range">最新净值日 {{ formatFundNavDate(latestNav?.trade_date) }}</span></div>
          <FundNavChart :points="nav" :loading="loadingNav" />
          <div class="chart-footnote"><span>按基金公司公布的每日净值展示</span><span>净值刷新间隔 {{ openNavRefreshIntervalSeconds }} 秒</span></div>
        </section>
      </template>
      <template v-else-if="selected">
        <section class="panel chart-panel fund-chart-panel">
          <div class="panel-heading"><div><span class="eyebrow">TODAY · 1 MINUTE</span><h3>场内分时行情</h3></div><span class="chart-range">{{ intraday.at(-1)?.trade_time?.slice(11, 16) || '等待分钟行情' }}</span></div>
          <IntradayChart :bars="intraday" :loading="loadingIntraday" :error="intradayError" empty-message="当前暂无分钟行情；休市时段或数据源不支持时会显示为空。" :previous-close="quote?.previous_close ?? 0" />
          <div class="chart-footnote"><span>成交量按股展示</span><span>分钟数据检查间隔 {{ Math.max(30, exchangeRefreshIntervalSeconds * 3) }} 秒</span></div>
        </section>
        <section class="panel chart-panel fund-chart-panel">
          <div class="panel-heading"><div><span class="eyebrow">DAILY PRICE</span><h3>日线走势</h3></div><span class="chart-range">{{ bars.length ? `${bars[0].trade_date} — ${bars[bars.length - 1].trade_date}` : '等待日线数据' }}</span></div>
          <MarketChart :bars="bars" :loading="loadingBars" />
          <div class="chart-footnote"><span>场内历史收盘价</span><span>选择基金或手动刷新时更新</span></div>
        </section>
      </template>
    </div>
  </div>
</template>
