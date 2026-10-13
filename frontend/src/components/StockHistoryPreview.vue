<script setup lang="ts">
import { computed } from 'vue'
import MarketChart from './MarketChart.vue'
import type { Bar } from '../types'

const props = defineProps<{
  symbol: string
  name: string
  bars: Bar[]
  loading: boolean
  error: string
}>()
const emit = defineEmits<{ close: [] }>()

const latest = computed(() => props.bars.at(-1))
const previous = computed(() => props.bars.at(-2))
const dailyReturn = computed(() => latest.value && previous.value?.close
  ? latest.value.close / previous.value.close - 1
  : null)
const recentRows = computed(() => props.bars.slice(-10).reverse())

function amount(value: number | null | undefined): string {
  return value === null || value === undefined
    ? '—'
    : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}
</script>

<template>
  <section class="stock-history-preview" :aria-label="`${name}近期日线数据`">
    <header class="preview-heading">
      <div>
        <span class="eyebrow">RECENT DAILY DATA</span>
        <h4><b>{{ symbol }}</b><span>{{ name }}</span></h4>
        <small>最近 30 个已收盘交易日</small>
      </div>
      <button type="button" class="preview-close" @click="emit('close')">收起 <span>×</span></button>
    </header>

    <div class="preview-metrics">
      <div><small>最近收盘</small><b>{{ amount(latest?.close) }}</b></div>
      <div><small>交易日</small><b>{{ latest?.trade_date ?? '—' }}</b></div>
      <div>
        <small>日涨跌</small>
        <b :class="dailyReturn === null ? '' : dailyReturn >= 0 ? 'up' : 'down'">
          {{ dailyReturn === null ? '—' : `${dailyReturn >= 0 ? '+' : ''}${(dailyReturn * 100).toFixed(2)}%` }}
        </b>
      </div>
    </div>

    <p v-if="error" class="preview-error" role="alert">{{ error }}</p>
    <MarketChart v-else :bars="bars" :loading="loading" />

    <div v-if="!loading && !error && recentRows.length" class="preview-table-scroll">
      <table class="preview-table">
        <thead><tr><th>日期</th><th>开盘</th><th>最高</th><th>最低</th><th>收盘</th><th>成交量</th></tr></thead>
        <tbody>
          <tr v-for="bar in recentRows" :key="bar.trade_date">
            <td>{{ bar.trade_date }}</td>
            <td>{{ amount(bar.open) }}</td>
            <td>{{ amount(bar.high) }}</td>
            <td>{{ amount(bar.low) }}</td>
            <td>{{ amount(bar.close) }}</td>
            <td>{{ bar.volume.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.stock-history-preview { margin-top: 12px; padding: 14px; border: 1px solid #dce7e1; border-radius: 5px; background: #fbfdfb; }
.preview-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.preview-heading h4 { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; margin: 4px 0 3px; color: #304139; font-size: 15px; }
.preview-heading h4 b { color: #14805c; font-family: var(--mono); }
.preview-heading > div > small { color: #73827b; font-size: 11px; }
.preview-close { display: inline-flex; align-items: center; gap: 5px; padding: 5px 8px; border: 1px solid #dce7e1; border-radius: 4px; background: #fff; color: #607169; font: inherit; font-size: 11px; cursor: pointer; }
.preview-close:hover { border-color: #8db8a1; color: #176f52; }
.preview-close span { font-size: 16px; line-height: .8; }
.preview-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }
.preview-metrics > div { display: grid; gap: 5px; min-width: 0; padding: 8px 10px; border: 1px solid #e5ece8; border-radius: 4px; background: #fff; }
.preview-metrics small { color: #74827b; font-size: 10px; }
.preview-metrics b { overflow: hidden; color: #33443b; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.preview-metrics b.up { color: #16805c; }
.preview-metrics b.down { color: #c54b43; }
.preview-error { margin: 12px 0; padding: 10px 12px; border: 1px solid #efd1cd; border-radius: 4px; background: #fff7f6; color: #a1433d; font-size: 12px; }
.preview-table-scroll { overflow-x: auto; margin-top: 10px; }
.preview-table { width: 100%; border-collapse: collapse; font-size: 11px; white-space: nowrap; }
.preview-table th { padding: 7px 9px; background: #f2f6f4; color: #66766e; text-align: left; font-weight: 500; }
.preview-table td { padding: 7px 9px; border-bottom: 1px solid #edf1ef; color: #405148; }
.preview-table td:first-child { font-family: var(--mono); }
:deep(.market-chart-wrap) { height: 270px; margin-top: 8px; }
@media (max-width: 500px) {
  .stock-history-preview { padding: 11px 9px; }
  .preview-metrics { gap: 5px; }
  .preview-metrics > div { padding: 7px 6px; }
  :deep(.market-chart-wrap) { height: 230px; }
}
</style>
