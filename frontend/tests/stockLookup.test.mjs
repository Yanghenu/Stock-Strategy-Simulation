import assert from 'node:assert/strict'
import test from 'node:test'
import { findExactStockName } from '../src/utils/stockLookup.ts'

const stocks = [
  { symbol: '600000', name: '浦发银行' },
  { symbol: '600001', name: '邯郸钢铁' },
]

test('returns the name only for an exact stock code', () => {
  assert.equal(findExactStockName(stocks, '600000'), '浦发银行')
  assert.equal(findExactStockName(stocks, '60000'), '')
})

test('returns an empty name when the code has no match', () => {
  assert.equal(findExactStockName(stocks, '688888'), '')
})
