import type { PaperSymbol } from '../types'

// 成本基础已经包含买入佣金，因此浮盈直接用持仓市值减成本基础。
export function paperSymbolPnl(item: Pick<PaperSymbol, 'shares' | 'last_price' | 'cost_basis'>): number {
  return item.shares * (item.last_price ?? 0) - item.cost_basis
}
