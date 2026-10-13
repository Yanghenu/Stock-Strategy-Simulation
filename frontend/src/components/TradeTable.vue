<script setup lang="ts">
import type { Trade } from '../types'

defineProps<{ trades: Trade[] }>()

// 成交明细按后端成交顺序展示，并同时展示信号日以便核对隔日执行规则。
</script>

<template>
  <div class="table-scroll">
    <table class="trade-table">
      <thead><tr><th>成交日</th><th>股票</th><th>信号日</th><th>方向</th><th>成交价</th><th>数量</th><th>成交额</th><th>费用</th></tr></thead>
      <tbody>
        <tr v-for="(trade, index) in trades" :key="`${trade.trade_date}-${index}`">
          <td>{{ trade.trade_date }}</td>
          <td>{{ trade.symbol || '—' }}</td>
          <td class="muted">{{ trade.signal_date }}</td>
          <td><span :class="['side-tag', trade.side === 'buy' ? 'up' : 'down']">{{ trade.side === 'buy' ? '买入' : '卖出' }}</span></td>
          <td>{{ trade.price.toFixed(2) }}</td>
          <td>{{ trade.quantity.toLocaleString('zh-CN') }}</td>
          <td>¥{{ trade.notional.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) }}</td>
          <td>¥{{ (trade.commission + trade.stamp_duty).toFixed(2) }}</td>
        </tr>
        <tr v-if="!trades.length"><td colspan="8" class="table-empty">暂无成交记录</td></tr>
      </tbody>
    </table>
  </div>
</template>
