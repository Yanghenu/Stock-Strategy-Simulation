<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { echarts } from '../charts'
import type { IntradayBar } from '../types'

const props = defineProps<{
  bars: IntradayBar[]
  loading: boolean
  error: string
  emptyMessage: string
  previousClose: number
}>()
const host = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

function render() {
  if (!chart) return
  const rows = props.bars
  const times = rows.map((bar) => bar.trade_time.slice(11, 16))
  const closingPrices = rows.map((bar) => bar.close)
  const volumes = rows.map((bar) => ({
    value: bar.volume,
    itemStyle: { color: bar.close >= bar.open ? '#16805c' : '#c54b43' },
  }))
  // 分时使用原始成交价；昨收线与实时快照同属原始价格口径，成交量统一按股显示。
  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    grid: [
      { left: 62, right: 24, top: 25, height: '60%' },
      { left: 62, right: 24, top: '76%', height: '13%' },
    ],
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: '#ffffff',
      borderColor: '#d8e2dd', textStyle: { color: '#263630', fontSize: 13 },
      extraCssText: 'box-shadow: 0 8px 24px rgba(28, 50, 40, .12);',
    },
    xAxis: [
      { type: 'category', data: times, boundaryGap: false, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisLabel: { color: '#65756e', fontSize: 12, hideOverlap: true }, axisTick: { show: false } },
      { type: 'category', gridIndex: 1, data: times, boundaryGap: true, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisLabel: { color: '#65756e', fontSize: 11, hideOverlap: true }, axisTick: { show: false } },
    ],
    yAxis: [
      { scale: true, splitNumber: 4, axisLabel: { color: '#65756e', fontSize: 12, formatter: (value: number) => value.toFixed(2) }, splitLine: { lineStyle: { color: '#e4ebe7' } }, axisLine: { show: false }, axisTick: { show: false } },
      { gridIndex: 1, scale: true, splitNumber: 2, axisLabel: { color: '#65756e', fontSize: 11, formatter: (value: number) => value >= 10000 ? `${(value / 10000).toFixed(0)}万` : `${value}` }, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false } },
    ],
    series: [
      { name: '分时价格', type: 'line', data: closingPrices, symbol: 'none', showSymbol: false, lineStyle: { color: '#16805c', width: 2 }, areaStyle: { color: 'rgba(22, 128, 92, .09)' }, emphasis: { focus: 'series' } },
      ...(props.previousClose > 0 ? [{ name: '昨收参考', type: 'line' as const, data: rows.map(() => props.previousClose), symbol: 'none', showSymbol: false, lineStyle: { color: '#9aa8a2', width: 1, type: 'dashed' as const }, tooltip: { show: false } }] : []),
      { name: '成交量（股）', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes, barMaxWidth: 8 },
    ],
  } as echarts.EChartsCoreOption, true)
}

onMounted(() => {
  if (!host.value) return
  chart = echarts.init(host.value, undefined, { renderer: 'canvas' })
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(host.value)
  render()
})
watch(() => [props.bars, props.previousClose], render, { deep: false })
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = undefined
})
</script>

<template>
  <div class="intraday-chart-wrap">
    <div v-if="loading && !bars.length" class="chart-overlay"><span class="spinner"></span> 正在载入今日分时</div>
    <div v-else-if="!bars.length" class="chart-overlay">{{ error || emptyMessage }}</div>
    <div ref="host" class="chart-host intraday-chart"></div>
  </div>
</template>
