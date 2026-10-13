import assert from 'node:assert/strict'
import test from 'node:test'
import { formatFundChange, formatFundNav, formatFundNavDate, fundTypeLabel } from '../src/utils/fundFormat.ts'

test('fund formats show placeholders for unpublished nav values', () => {
  assert.equal(formatFundNavDate(null), '—')
  assert.equal(formatFundNav(undefined), '—')
})

test('fund formats render nav, fractional return and type labels', () => {
  assert.equal(formatFundNav(1.23456), '1.2346')
  assert.equal(formatFundChange(0.0125), '+1.25%')
  assert.equal(formatFundChange(-0.003), '-0.30%')
  assert.equal(fundTypeLabel('etf'), 'ETF')
  assert.equal(fundTypeLabel('lof'), 'LOF')
  assert.equal(fundTypeLabel('open_end'), '开放式基金')
})
