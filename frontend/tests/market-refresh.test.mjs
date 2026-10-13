import assert from 'node:assert/strict'
import test from 'node:test'
import { intradayRefreshIntervalMs } from '../src/utils/marketRefresh.ts'

test('intraday refresh interval defaults to and never falls below two seconds', () => {
  assert.equal(intradayRefreshIntervalMs(undefined), 2000)
  assert.equal(intradayRefreshIntervalMs(1), 2000)
  assert.equal(intradayRefreshIntervalMs(2), 2000)
  assert.equal(intradayRefreshIntervalMs(4), 4000)
})
