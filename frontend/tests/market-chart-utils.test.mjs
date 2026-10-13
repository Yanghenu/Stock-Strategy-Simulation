import assert from 'node:assert/strict'
import test from 'node:test'
import { mergeLiveDailyBar } from '../src/market-chart-utils.ts'

const yesterday = {
  trade_date: '2026-10-12', open: 7.2, high: 7.7, low: 7.1, close: 7.5,
  volume: 1000, turnover: 7500,
}
const quote = {
  symbol: '600000', name: '浦发银行', latest_price: 10.2, change_amount: 0.2,
  change_percent: 2, open: 10, high: 10.5, low: 9.8, previous_close: 10,
  volume_hands: 3, turnover: 3015, source: 'test', fetched_at: '2026-10-13T10:02:00+08:00', stale: false,
}

test("aggregates current minute bars into a qfq daily candle and replaces today's cached candle", () => {
  const existingToday = { ...yesterday, trade_date: '2026-10-13', close: 7.4 }
  const minutes = [
    { trade_time: '2026-10-13T10:01:00+08:00', open: 10, high: 10.3, low: 9.9, close: 10.1, volume: 100, turnover: 1005 },
    { trade_time: '2026-10-13T10:02:00+08:00', open: 10.1, high: 10.5, low: 9.8, close: 10.2, volume: 200, turnover: 2010 },
  ]

  const result = mergeLiveDailyBar([yesterday, existingToday], minutes, quote, '2026-10-13')

  assert.deepEqual(result.map((bar) => bar.trade_date), ['2026-10-12', '2026-10-13'])
  const candle = result[1]
  assert.ok(Math.abs(candle.open - 7.5) < 1e-9)
  assert.ok(Math.abs(candle.high - 7.875) < 1e-9)
  assert.ok(Math.abs(candle.low - 7.35) < 1e-9)
  assert.ok(Math.abs(candle.close - 7.65) < 1e-9)
  assert.equal(candle.volume, 300)
  assert.equal(candle.turnover, 3015)
})

test('keeps historical candles unchanged when the minute feed has no bars for today', () => {
  const result = mergeLiveDailyBar([yesterday], [], quote, '2026-10-13')

  assert.deepEqual(result, [yesterday])
})
