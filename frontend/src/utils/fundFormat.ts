import type { FundType } from '../types'

/** 未公布净值时用短横线，避免页面把缺数据误显示成零。 */
export function formatFundNavDate(value: string | null | undefined): string {
  return value || '—'
}

export function formatFundNav(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? '—' : value.toFixed(4)
}

export function formatFundChange(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

export function fundTypeLabel(value: FundType): string {
  return ({ etf: 'ETF', lof: 'LOF', open_end: '开放式基金' })[value]
}
