import assert from 'node:assert/strict'
import test from 'node:test'
import { fundSearchTerm } from '../src/utils/fundSearch.ts'

test('fund search skips blank input and trims a supplied code or name', () => {
  assert.equal(fundSearchTerm(''), null)
  assert.equal(fundSearchTerm('   '), null)
  assert.equal(fundSearchTerm(' 510300 '), '510300')
  assert.equal(fundSearchTerm(' 华夏成长 '), '华夏成长')
})
