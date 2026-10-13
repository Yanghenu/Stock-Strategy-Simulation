import assert from 'node:assert/strict'
import test from 'node:test'
import { api } from '../src/api/client.ts'
import { paperSymbolPnl } from '../src/utils/paperExperiment.ts'

const calls = []
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url: String(url), method: options.method || 'GET', body: options.body })
  return { ok: true, json: async () => ({}) }
}

test('paper experiment client uses task endpoints and encodes task ids', async () => {
  calls.length = 0
  const task = { task_name: '对比', symbols: ['600000'], strategy_code: 'dual_ma', initial_capital: 100000 }

  await api.getPaperStrategies()
  await api.listPaperSessions()
  await api.createPaperSession(task)
  await api.getPaperSession('task/1')
  await api.stopPaperSession('task/1')

  assert.deepEqual(calls.map(({ url, method }) => [url, method]), [
    ['/api/paper-trading/strategies', 'GET'],
    ['/api/paper-trading/sessions', 'GET'],
    ['/api/paper-trading/sessions', 'POST'],
    ['/api/paper-trading/sessions/task%2F1', 'GET'],
    ['/api/paper-trading/sessions/task%2F1/stop', 'POST'],
  ])
  assert.equal(calls[2].body, JSON.stringify(task))
})

test('paper holding profit subtracts the full cost basis', () => {
  assert.equal(paperSymbolPnl({ shares: 100, last_price: 12, cost_basis: 1000 }), 200)
  assert.equal(paperSymbolPnl({ shares: 0, last_price: null, cost_basis: 0 }), 0)
})

test('fund market and paper clients use type-scoped routes and SQL-backed task endpoints', async () => {
  calls.length = 0
  const task = { task_name: 'ETF 对照', fund_type: 'etf', symbols: ['510300'], strategy_code: 'dual_ma', initial_capital: 100000, parameters: {} }

  await api.searchFunds('沪深 300', 'etf')
  await api.getFundQuote('open_end', '000001', true)
  await api.getFundBars('lof', '161130', '2025-01-01', '2025-12-31')
  await api.getFundIntraday('etf', '510300')
  await api.getFundNav('open_end', '000001')
  await api.getFundPaperStrategies()
  await api.listFundPaperSessions()
  await api.createFundPaperSession(task)
  await api.getFundPaperSession('fund/task')
  await api.stopFundPaperSession('fund/task')

  assert.deepEqual(calls.map(({ url, method }) => [url, method]), [
    ['/api/funds/search?q=%E6%B2%AA%E6%B7%B1%20300&fund_type=etf', 'GET'],
    ['/api/funds/open_end/000001/quote?force_refresh=true', 'GET'],
    ['/api/funds/lof/161130/bars?start_date=2025-01-01&end_date=2025-12-31', 'GET'],
    ['/api/funds/etf/510300/intraday', 'GET'],
    ['/api/funds/open_end/000001/nav', 'GET'],
    ['/api/fund-paper/strategies', 'GET'],
    ['/api/fund-paper/sessions', 'GET'],
    ['/api/fund-paper/sessions', 'POST'],
    ['/api/fund-paper/sessions/fund%2Ftask', 'GET'],
    ['/api/fund-paper/sessions/fund%2Ftask/stop', 'POST'],
  ])
  assert.equal(calls[7].body, JSON.stringify(task))
})
