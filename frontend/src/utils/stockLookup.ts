import type { Quote } from '../types'

/** 只接受完整代码匹配的行情记录，避免输入前缀时显示到其他股票名称。 */
export function findExactStockName(quotes: readonly Quote[], symbol: string): string {
  return quotes.find((quote) => quote.symbol === symbol)?.name ?? ''
}
