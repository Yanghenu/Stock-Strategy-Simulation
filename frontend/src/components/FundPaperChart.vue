<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { echarts } from '../charts'
import type { FundPaperEquityPoint } from '../types'

const props = defineProps<{ points: FundPaperEquityPoint[] }>()
const host = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

function render() {
  if (!chart) return
  const rows = props.points
  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 65, right: 55, top: 36, bottom: 48 },
    legend: { top: 1, textStyle: { color: '#52645b', fontSize: 12 } },
    tooltip: {
      trigger: 'axis', backgroundColor: '#fff', borderColor: '#d8e2dd',
      textStyle: { color: '#263630', fontSize: 13 },
      formatter: (items: unknown) => {
        const rows = items as Array<{ seriesName: string; axisValue: string; value: number }>
        return `${rows[0]?.axisValue || ''}<br>${rows.map((item) => `${item.seriesName}：${item.seriesName === '回撤' ? `${(Number(item.value) * 100).toFixed(2)}%` : `¥${Number(item.value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`}`).join('<br>')}`
      },
    },
    xAxis: { type: 'category', data: rows.map((point) => point.trade_date), axisLabel: { color: '#65756e', fontSize: 12, hideOverlap: true }, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisTick: { show: false } },
    yAxis: [
      { type: 'value', scale: true, axisLabel: { color: '#65756e', fontSize: 12, formatter: (value: number) => `${(value / 10000).toFixed(1)}万` }, splitLine: { lineStyle: { color: '#e4ebe7' } } },
      { type: 'value', scale: true, inverse: true, min: 0, max: 1, axisLabel: { color: '#9a665f', fontSize: 12, formatter: (value: number) => `${(value * 100).toFixed(0)}%` }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', bottom: 4, height: 17, borderColor: '#d8e2dd', backgroundColor: '#f2f6f4', fillerColor: 'rgba(22,128,92,.13)', textStyle: { color: '#65756e', fontSize: 11 } },
    ],
    series: [
      { name: '账户净值', type: 'line', data: rows.map((point) => point.equity), showSymbol: false, lineStyle: { color: '#16805c', width: 2 }, areaStyle: { color: 'rgba(22,128,92,.08)' } },
      { name: '回撤', type: 'line', yAxisIndex: 1, data: rows.map((point) => point.drawdown), showSymbol: false, lineStyle: { color: '#c65a4c', width: 1.5 } },
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
watch(() => props.points, render)
onBeforeUnmount(() => { resizeObserver?.disconnect(); chart?.dispose(); chart = undefined })
</script>

<template>
  <div class="fund-paper-chart-wrap">
    <div v-if="!points.length" class="chart-overlay">账户曲线将在实验处理后显示</div>
    <div ref="host" class="chart-host fund-paper-chart"></div>
  </div>
</template>
