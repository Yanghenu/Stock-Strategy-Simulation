<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { echarts } from '../charts'
import type { Bar } from '../types'

const props = defineProps<{ bars: Bar[]; loading: boolean }>()
const host = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

function render() {
  if (!chart) return
  const rows = props.bars
  const dates = rows.map((bar) => bar.trade_date)
  const candles = rows.map((bar) => [bar.open, bar.close, bar.low, bar.high])
  const volumes = rows.map((bar) => ({
    value: bar.volume,
    itemStyle: { color: bar.close >= bar.open ? '#16805c' : '#c54b43' },
  }))
  // 白色主题使用低对比度网格和深色标签；交易日、蜡烛与成交量缩放时继续保持横轴联动。
  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    grid: [
      { left: 55, right: 24, top: 28, height: '63%' },
      { left: 55, right: 24, top: '77%', height: '13%' },
    ],
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: '#ffffff', borderColor: '#d8e2dd', textStyle: { color: '#263630', fontSize: 13 }, extraCssText: 'box-shadow: 0 8px 24px rgba(28, 50, 40, .12);' },
    xAxis: [
      { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisLabel: { color: '#65756e', fontSize: 12, hideOverlap: true }, axisTick: { show: false }, min: 'dataMin', max: 'dataMax' },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisLabel: { show: false }, axisTick: { show: false }, min: 'dataMin', max: 'dataMax' },
    ],
    yAxis: [
      { scale: true, splitNumber: 4, axisLabel: { color: '#65756e', fontSize: 12 }, splitLine: { lineStyle: { color: '#e4ebe7' } }, axisLine: { show: false }, axisTick: { show: false } },
      { gridIndex: 1, scale: true, splitNumber: 2, axisLabel: { color: '#65756e', fontSize: 12, formatter: (value: number) => value >= 10000 ? `${(value / 10000).toFixed(0)}万` : `${value}` }, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 18, borderColor: '#d8e2dd', backgroundColor: '#f2f6f4', fillerColor: 'rgba(22, 128, 92, .14)', handleStyle: { color: '#16805c' }, textStyle: { color: '#65756e', fontSize: 12 } },
    ],
    series: [
      { name: '前复权日线', type: 'candlestick', data: candles, itemStyle: { color: '#16805c', color0: '#c54b43', borderColor: '#16805c', borderColor0: '#c54b43' }, emphasis: { itemStyle: { borderWidth: 1 } } },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes, barMaxWidth: 8 },
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
watch(() => props.bars, render, { deep: false })
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = undefined
})
</script>

<template>
  <div class="market-chart-wrap">
    <div v-if="loading" class="chart-overlay"><span class="spinner"></span> 正在载入日线</div>
    <div v-else-if="!bars.length" class="chart-overlay">所选区间暂无日线数据</div>
    <div ref="host" class="chart-host market-chart"></div>
  </div>
</template>
