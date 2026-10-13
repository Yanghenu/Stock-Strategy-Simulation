<script setup lang="ts">
import type { Quote } from '../types'

defineProps<{
  modelValue: string
  results: Quote[]
  searching: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
  select: [quote: Quote]
}>()

// 输入框允许按代码或简称搜索，选择结果后由父级统一刷新行情与日线。
function updateValue(event: Event) {
  emit('update:modelValue', (event.target as HTMLInputElement).value)
}
</script>

<template>
  <div class="stock-search">
    <label class="search-field">
      <span class="search-glyph">⌕</span>
      <input :value="modelValue" placeholder="搜索股票代码或名称" @input="updateValue" />
      <span class="search-hint">⌘ K</span>
    </label>
    <div v-if="searching || results.length" class="search-results">
      <div v-if="searching" class="search-empty">正在检索行情…</div>
      <button
        v-for="item in results"
        :key="item.symbol"
        class="search-result"
        type="button"
        @click="emit('select', item)"
      >
        <span class="result-symbol">{{ item.symbol }}</span>
        <span class="result-name">{{ item.name }}</span>
        <span class="result-price">{{ item.latest_price.toFixed(2) }}</span>
      </button>
      <div v-if="!searching && !results.length" class="search-empty">没有匹配的股票</div>
    </div>
  </div>
</template>
