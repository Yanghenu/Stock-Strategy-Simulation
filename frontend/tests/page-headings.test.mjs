import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const pageFiles = [
  '../src/App.vue',
  '../src/components/PaperExperimentCenter.vue',
  '../src/components/FundMarket.vue',
  '../src/components/FundExperimentCenter.vue',
]

test('all dashboard pages omit oversized slogan headings', () => {
  for (const pageFile of pageFiles) {
    const source = readFileSync(fileURLToPath(new URL(pageFile, import.meta.url)), 'utf8')
    assert.doesNotMatch(source, /<h1>/, `${pageFile} still has a large page heading`)
  }
})
