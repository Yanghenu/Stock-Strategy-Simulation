<script setup lang="ts">
import { computed } from 'vue'
import type { Quote } from '../types'

const props = defineProps<{ quote: Quote | null; loading: boolean; watched: boolean; watchPending: boolean }>()
defineEmits<{ toggleWatch: [] }>()
const tone = computed(() => (props.quote?.change_percent ?? 0) >= 0 ? 'up' : 'down')
const fetchedAt = computed(() => props.quote ? new Date(props.quote.fetched_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '--:--')
// 备用行情源不提供当日 OHLC 时将 0 显示为缺失标记，避免误导为真实价格。
const priceText = (value: number) => value > 0 ? value.toFixed(2) : '—'

// 行情概览只显示行情服务返回的快照字段，并显式标注缓存过期状态。
</script>

<template>
  <section v-if="quote" class="quote-card">
    <div class="quote-main">
      <div class="quote-name-line">
        <div>
          <span class="eyebrow">实时行情 · {{ quote.symbol }}</span>
          <h2>{{ quote.name }}</h2>
        </div>
        <span :class="['freshness', quote.stale ? 'is-stale' : '']">
          <i></i>{{ quote.stale ? '缓存行情' : '行情已同步' }}
        </span>
        <button
          class="watch-action"
          :class="{ 'is-following': watched }"
          :disabled="watchPending"
          :aria-pressed="watched"
          :aria-label="watched ? `取消关注${quote.name}` : `关注${quote.name}`"
          @click="$emit('toggleWatch')"
        >
          {{ watchPending ? '保存中…' : watched ? '✓ 已关注' : '＋ 关注' }}
        </button>
      </div>
      <div class="quote-value-line">
        <strong :class="tone">{{ quote.latest_price.toFixed(2) }}</strong>
        <div :class="['change-chip', tone]">
          {{ quote.change_amount >= 0 ? '+' : '' }}{{ quote.change_amount.toFixed(2) }}
          <span>{{ quote.change_percent >= 0 ? '+' : '' }}{{ quote.change_percent.toFixed(2) }}%</span>
        </div>
      </div>
      <div class="quote-meta">更新于 {{ fetchedAt }} · {{ quote.source }}</div>
    </div>
    <div class="quote-stats">
      <div><span>今开</span><b>{{ priceText(quote.open) }}</b></div>
      <div><span>最高</span><b class="up">{{ priceText(quote.high) }}</b></div>
      <div><span>最低</span><b class="down">{{ priceText(quote.low) }}</b></div>
      <div><span>昨收</span><b>{{ priceText(quote.previous_close) }}</b></div>
      <div><span>成交额</span><b>{{ (quote.turnover / 100000000).toFixed(2) }}<small> 亿</small></b></div>
      <div><span>成交量</span><b>{{ (quote.volume_hands / 10000).toFixed(2) }}<small> 万手</small></b></div>
    </div>
  </section>
  <section v-else class="quote-placeholder">
    <span class="placeholder-mark">◌</span>
    <span>{{ loading ? '正在读取实时快照…' : '选择一只股票查看行情' }}</span>
  </section>
</template>
