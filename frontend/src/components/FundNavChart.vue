<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { echarts } from '../charts'
import type { FundNavPoint } from '../types'

const props = defineProps<{ points: FundNavPoint[]; loading: boolean }>()
const host = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

function render() {
  if (!chart) return
  const rows = props.points
  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 58, right: 24, top: 35, bottom: 48 },
    legend: { top: 2, textStyle: { color: '#52645b', fontSize: 12 } },
    tooltip: {
      trigger: 'axis', backgroundColor: '#fff', borderColor: '#d8e2dd',
      textStyle: { color: '#263630', fontSize: 13 },
      valueFormatter: (value: unknown) => Number(value).toFixed(4),
    },
    xAxis: { type: 'category', data: rows.map((point) => point.trade_date), axisLabel: { color: '#65756e', fontSize: 12, hideOverlap: true }, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisTick: { show: false } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#65756e', fontSize: 12 }, splitLine: { lineStyle: { color: '#e4ebe7' } } },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', bottom: 4, height: 17, borderColor: '#d8e2dd', backgroundColor: '#f2f6f4', fillerColor: 'rgba(22,128,92,.13)', textStyle: { color: '#65756e', fontSize: 11 } },
    ],
    series: [
      { name: '单位净值', type: 'line', data: rows.map((point) => point.unit_nav), showSymbol: false, lineStyle: { color: '#16805c', width: 2 }, areaStyle: { color: 'rgba(22,128,92,.08)' } },
      { name: '累计净值', type: 'line', data: rows.map((point) => point.accumulated_nav), showSymbol: false, connectNulls: true, lineStyle: { color: '#587fd1', width: 1.6 } },
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
  <div class="fund-nav-chart-wrap">
    <div v-if="loading && !points.length" class="chart-overlay"><span class="spinner"></span> 正在载入净值</div>
    <div v-else-if="!points.length" class="chart-overlay">暂无已公布净值</div>
    <div ref="host" class="chart-host fund-nav-chart"></div>
  </div>
</template>
