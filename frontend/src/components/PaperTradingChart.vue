<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { echarts } from '../charts'
import type { PaperSession } from '../types'

const props = defineProps<{ session: PaperSession | null }>()
const host = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

function render() {
  const curve = props.session?.equity_curve || []
  if (!chart || !curve.length) return
  const dates = curve.map((point) => point.trade_date)
  const buyDates = new Set((props.session?.trades || []).filter((trade) => trade.side === 'buy').map((trade) => trade.trade_date))
  const sellDates = new Set((props.session?.trades || []).filter((trade) => trade.side === 'sell').map((trade) => trade.trade_date))
  const marks = curve.flatMap((point) => {
    if (buyDates.has(point.trade_date)) return [{ coord: [point.trade_date, point.equity], value: '买', itemStyle: { color: '#16805c' } }]
    if (sellDates.has(point.trade_date)) return [{ coord: [point.trade_date, point.equity], value: '卖', itemStyle: { color: '#c54b43' } }]
    return []
  })
  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    grid: [{ left: 62, right: 24, top: 42, height: '55%' }, { left: 62, right: 24, top: '76%', height: '13%' }],
    legend: { top: 4, textStyle: { color: '#52635c', fontSize: 13 }, itemWidth: 12, itemHeight: 7 },
    tooltip: { trigger: 'axis', backgroundColor: '#ffffff', borderColor: '#d8e2dd', textStyle: { color: '#263630', fontSize: 13 } },
    xAxis: [
      { type: 'category', data: dates, boundaryGap: false, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisLabel: { color: '#65756e', fontSize: 12, hideOverlap: true }, axisTick: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisLabel: { color: '#65756e', fontSize: 12, hideOverlap: true }, axisTick: { show: false } },
    ],
    yAxis: [
      { type: 'value', scale: true, axisLabel: { color: '#65756e', fontSize: 12, formatter: (value: number) => `${(value / 10000).toFixed(1)}万` }, splitLine: { lineStyle: { color: '#e4ebe7' } }, axisLine: { show: false }, axisTick: { show: false } },
      { type: 'value', gridIndex: 1, min: (value: { min: number }) => Math.min(-0.01, value.min), max: 0, axisLabel: { color: '#65756e', fontSize: 12, formatter: (value: number) => `${(value * 100).toFixed(0)}%` }, splitLine: { lineStyle: { color: '#e4ebe7' } }, axisLine: { show: false }, axisTick: { show: false } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }],
    series: [
      { name: '账户净值', type: 'line', data: curve.map((point) => point.equity), showSymbol: false, lineStyle: { color: '#16805c', width: 2 }, areaStyle: { color: 'rgba(22, 128, 92, .12)' }, markPoint: { symbol: 'pin', symbolSize: 36, label: { color: '#ffffff', fontWeight: 700 }, data: marks } },
      { name: '回撤', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: curve.map((point) => point.drawdown), showSymbol: false, lineStyle: { color: '#c54b43', width: 1.5 }, areaStyle: { color: 'rgba(197, 75, 67, .12)' } },
    ],
  } as echarts.EChartsCoreOption, true)
}

async function initializeChart() {
  await nextTick()
  if (!props.session?.equity_curve?.length || !host.value || chart) return
  chart = echarts.init(host.value, undefined, { renderer: 'canvas' })
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(host.value)
  render()
}

function disposeChart() {
  resizeObserver?.disconnect()
  resizeObserver = undefined
  chart?.dispose()
  chart = undefined
}

watch(() => Boolean(props.session?.equity_curve?.length), (hasCurve) => {
  if (hasCurve) void initializeChart()
  else disposeChart()
}, { immediate: true })
watch(() => props.session, render, { flush: 'post' })
onBeforeUnmount(disposeChart)
</script>

<template>
  <div v-if="session?.equity_curve?.length" ref="host" class="chart-host paper-chart"></div>
  <div v-else class="chart-empty">首次有效行情更新后显示净值曲线</div>
</template>
