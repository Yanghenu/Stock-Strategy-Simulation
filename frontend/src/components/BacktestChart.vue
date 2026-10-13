<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { echarts } from '../charts'
import type { BacktestRecord } from '../types'

const props = defineProps<{ result: BacktestRecord | null }>()
const host = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | undefined
let resizeObserver: ResizeObserver | undefined

function render() {
  if (!chart || !props.result) return
  const curve = props.result.equity_curve || []
  // 轮动策略的基准覆盖整组股票，图例需说明等权买入持有的含义。
  const benchmarkName = props.result.strategy_code === 'momentum_rotation' ? '股票池等权买入持有' : '买入持有'
  const dates = curve.map((point) => point.trade_date)
  const equityByDate = new Map(curve.map((point) => [point.trade_date, point.equity]))
  const markers = (props.result.trades || []).flatMap((trade) => {
    const equity = equityByDate.get(trade.trade_date)
    return equity === undefined ? [] : [{
      coord: [trade.trade_date, equity],
      value: trade.side === 'buy' ? '买' : '卖',
      itemStyle: { color: trade.side === 'buy' ? '#16805c' : '#c54b43' },
    }]
  })
  // 浅色主题下保持策略、基准和回撤颜色区分，并让网格及提示文字在白底上清晰可读。
  // 策略净值、基准和回撤共享回测曲线日期；买卖点只标在实际成交日。
  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    grid: [{ left: 62, right: 24, top: 40, height: '56%' }, { left: 62, right: 24, top: '76%', height: '13%' }],
    legend: { top: 4, textStyle: { color: '#52635c', fontSize: 13 }, itemWidth: 12, itemHeight: 7 },
    tooltip: { trigger: 'axis', backgroundColor: '#ffffff', borderColor: '#d8e2dd', textStyle: { color: '#263630', fontSize: 13 }, extraCssText: 'box-shadow: 0 8px 24px rgba(28, 50, 40, .12);' },
    xAxis: [
      { type: 'category', data: dates, boundaryGap: false, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisLabel: { color: '#65756e', fontSize: 12, hideOverlap: true }, axisTick: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLine: { lineStyle: { color: '#d8e2dd' } }, axisLabel: { color: '#65756e', fontSize: 12, hideOverlap: true }, axisTick: { show: false } },
    ],
    yAxis: [
      { type: 'value', scale: true, axisLabel: { color: '#65756e', fontSize: 12, formatter: (value: number) => `${(value / 10000).toFixed(0)}万` }, splitLine: { lineStyle: { color: '#e4ebe7' } }, axisLine: { show: false }, axisTick: { show: false } },
      { type: 'value', gridIndex: 1, min: (value: { min: number }) => Math.min(-0.01, value.min), max: 0, axisLabel: { color: '#65756e', fontSize: 12, formatter: (value: number) => `${(value * 100).toFixed(0)}%` }, splitLine: { lineStyle: { color: '#e4ebe7' } }, axisLine: { show: false }, axisTick: { show: false } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }],
    series: [
      { name: '策略净值', type: 'line', data: curve.map((point) => point.equity), showSymbol: false, lineStyle: { color: '#16805c', width: 2 }, areaStyle: { color: 'rgba(22, 128, 92, .12)' }, markPoint: { symbol: 'pin', symbolSize: 36, label: { color: '#ffffff', fontWeight: 700 }, data: markers } },
      { name: benchmarkName, type: 'line', data: curve.map((point) => point.benchmark_equity), showSymbol: false, lineStyle: { color: '#8b9a93', width: 1.5, type: 'dashed' } },
      { name: '回撤', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: curve.map((point) => point.drawdown), showSymbol: false, lineStyle: { color: '#c54b43', width: 1.5 }, areaStyle: { color: 'rgba(197, 75, 67, .12)' } },
    ],
  } as echarts.EChartsCoreOption, true)
}

async function initializeChart() {
  // 回测曲线由后台异步返回；等待 v-if 容器挂载后再初始化 ECharts。
  await nextTick()
  if (!props.result?.equity_curve?.length || !host.value || chart) return
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

// 容器由曲线数据控制显示，分别处理首次已有结果与后台稍后返回结果两种时序。
watch(() => Boolean(props.result?.equity_curve?.length), (hasCurve) => {
  if (hasCurve) void initializeChart()
  else disposeChart()
}, { immediate: true })
watch(() => props.result, render, { flush: 'post' })
onBeforeUnmount(() => {
  disposeChart()
})
</script>

<template>
  <div v-if="result?.equity_curve?.length" ref="host" class="chart-host backtest-chart"></div>
  <div v-else class="chart-empty">回测完成后，策略净值、买入持有基准与回撤将在此展示</div>
</template>
