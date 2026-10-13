import type { Bar, IntradayBar, Quote } from './types'

/**
 * 用当天分钟线合成实时日 K，并换算到历史图表使用的前复权价格口径。
 * 历史 K 线仍使用缓存结果，因此轮询不会反复读取一年日线。
 */
export function mergeLiveDailyBar(
  bars: Bar[],
  intraday: IntradayBar[],
  quote: Pick<Quote, 'previous_close'> | null,
  today: string,
): Bar[] {
  const minutes = intraday
    .filter((bar) => bar.trade_time.slice(0, 10) === today)
    .sort((left, right) => left.trade_time.localeCompare(right.trade_time))
  if (!minutes.length) return bars

  const previousBar = [...bars].reverse().find((bar) => bar.trade_date < today)
  const previousClose = quote?.previous_close ?? 0
  const factor = previousBar && previousClose > 0 ? previousBar.close / previousClose : 1
  const raw = {
    open: minutes[0].open,
    high: Math.max(...minutes.map((bar) => bar.high)),
    low: Math.min(...minutes.map((bar) => bar.low)),
    close: minutes[minutes.length - 1].close,
  }
  const liveBar: Bar = {
    trade_date: today,
    open: raw.open * factor,
    high: raw.high * factor,
    low: raw.low * factor,
    close: raw.close * factor,
    volume: minutes.reduce((total, bar) => total + bar.volume, 0),
    turnover: minutes.reduce((total, bar) => total + bar.turnover, 0),
  }

  return [...bars.filter((bar) => bar.trade_date !== today), liveBar]
    .sort((left, right) => left.trade_date.localeCompare(right.trade_date))
}

/**
 * 汇总指定交易日的分钟线 OHLC，补充实时快照未提供的今开、最高和最低。
 * 分时接口按交易时间返回逐分钟价格，因此首分钟开盘价及区间高低点可直接用于行情卡片。
 */
export function aggregateIntradayOhlc(
  intraday: IntradayBar[],
  today: string,
): Pick<Quote, 'open' | 'high' | 'low'> | null {
  const minutes = intraday
    .filter((bar) => bar.trade_time.slice(0, 10) === today)
    .sort((left, right) => left.trade_time.localeCompare(right.trade_time))
  if (!minutes.length) return null

  let high = minutes[0].high
  let low = minutes[0].low
  for (const bar of minutes.slice(1)) {
    high = Math.max(high, bar.high)
    low = Math.min(low, bar.low)
  }
  return { open: minutes[0].open, high, low }
}
