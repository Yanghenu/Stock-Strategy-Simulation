import type {
  AppConfig,
  BacktestList,
  BacktestRecord,
  BacktestRequest,
  Bar,
  FundBar,
  FundIntradayBar,
  FundInstrument,
  FundNavPoint,
  FundPaperCreateRequest,
  FundPaperStrategy,
  FundPaperSession,
  FundPaperSessionSummary,
  FundQuote,
  FundType,
  IntradayBar,
  PaperSessionSummary,
  PaperSession,
  PaperStrategy,
  PaperTaskCreateRequest,
  Quote,
  WatchedStock,
} from '../types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  // 统一处理 JSON 与 API 错误消息，避免各页面重复编写网络边界逻辑。
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  const payload = await response.json().catch(() => ({})) as { detail?: string; message?: string }
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `请求失败（${response.status}）`)
  }
  return payload as T
}

export const api = {
  // 所有路径都走 Vite 的 /api 代理；查询字符串和值统一编码。
  getConfig: () => request<AppConfig>('/api/app-config'),
  searchStocks: (query: string) => request<Quote[]>(`/api/stocks/search?q=${encodeURIComponent(query)}`),
  getQuote: (symbol: string, forceRefresh = false) => request<Quote>(
    `/api/market/quotes/${encodeURIComponent(symbol)}${forceRefresh ? '?force_refresh=true' : ''}`,
  ),
  getBars: (symbol: string, start: string, end: string) => request<Bar[]>(
    `/api/stocks/${encodeURIComponent(symbol)}/bars?start_date=${start}&end_date=${end}&adjust=qfq`,
  ),
  getIntradayBars: (symbol: string) => request<IntradayBar[]>(
    `/api/stocks/${encodeURIComponent(symbol)}/intraday`,
  ),
  // 基金 ETF/LOF 和开放基金分别使用场内报价/净值接口，路由中明确传递基金类型。
  searchFunds: (query: string, fundType?: FundType) => request<FundInstrument[]>(
    `/api/funds/search?q=${encodeURIComponent(query)}${fundType ? `&fund_type=${fundType}` : ''}`,
  ),
  getFundQuote: (fundType: FundType, symbol: string, forceRefresh = false) => request<FundQuote>(
    `/api/funds/${fundType}/${encodeURIComponent(symbol)}/quote${forceRefresh ? '?force_refresh=true' : ''}`,
  ),
  getFundBars: (fundType: FundType, symbol: string, start: string, end: string) => request<FundBar[]>(
    `/api/funds/${fundType}/${encodeURIComponent(symbol)}/bars?start_date=${start}&end_date=${end}`,
  ),
  getFundIntraday: (fundType: FundType, symbol: string) => request<FundIntradayBar[]>(
    `/api/funds/${fundType}/${encodeURIComponent(symbol)}/intraday`,
  ),
  getFundNav: (fundType: FundType, symbol: string, start?: string, end?: string) => request<FundNavPoint[]>(
    `/api/funds/${fundType}/${encodeURIComponent(symbol)}/nav${start || end ? `?${new URLSearchParams({ ...(start ? { start_date: start } : {}), ...(end ? { end_date: end } : {}) })}` : ''}`,
  ),
  getWatchlist: () => request<WatchedStock[]>('/api/watchlist'),
  addWatchlist: (item: WatchedStock) => request<WatchedStock>('/api/watchlist', {
    method: 'POST', body: JSON.stringify(item),
  }),
  removeWatchlist: (symbol: string) => request<void>(`/api/watchlist/${encodeURIComponent(symbol)}`, {
    method: 'DELETE',
  }),
  submitBacktest: (body: BacktestRequest) => request<{ run_id: string; status: string }>('/api/backtests', {
    method: 'POST', body: JSON.stringify(body),
  }),
  getBacktestStrategies: async () => {
    try {
      return await request<PaperStrategy[]>('/api/backtests/strategies')
    } catch (error) {
      // 旧后端会把 /strategies 当作回测 ID，可能返回 500；同时保留原错误供排查。
      const detail = error instanceof Error ? error.message : '未知错误'
      throw new Error(`回测策略目录暂不可用，请确认后端已重启（${detail}）`)
    }
  },
  getBacktest: (runId: string) => request<BacktestRecord>(`/api/backtests/${encodeURIComponent(runId)}`),
  listBacktests: () => request<BacktestList>('/api/backtests?offset=0&limit=50'),
  cancelBacktest: (runId: string) => request<{ run_id: string; requested: boolean }>(
    `/api/backtests/${encodeURIComponent(runId)}/cancel`, { method: 'POST' },
  ),
  startPaperTrading: (initialCapital: number) => request<{ session_id: string; status: string }>(
    '/api/paper-trading/start', { method: 'POST', body: JSON.stringify({ initial_capital: initialCapital }) },
  ),
  getCurrentPaperTrading: () => request<PaperSession | null>('/api/paper-trading/current'),
  getPaperStrategies: () => request<PaperStrategy[]>('/api/paper-trading/strategies'),
  listPaperSessions: () => request<PaperSessionSummary[]>('/api/paper-trading/sessions'),
  createPaperSession: (body: PaperTaskCreateRequest) => request<{ session_id: string; status: string }>(
    '/api/paper-trading/sessions', { method: 'POST', body: JSON.stringify(body) },
  ),
  getPaperSession: (sessionId: string) => request<PaperSession>(
    `/api/paper-trading/sessions/${encodeURIComponent(sessionId)}`,
  ),
  stopPaperSession: (sessionId: string) => request<{ session_id: string; status: string }>(
    `/api/paper-trading/sessions/${encodeURIComponent(sessionId)}/stop`, { method: 'POST' },
  ),
  getFundPaperStrategies: () => request<FundPaperStrategy[]>('/api/fund-paper/strategies'),
  listFundPaperSessions: () => request<FundPaperSessionSummary[]>('/api/fund-paper/sessions'),
  createFundPaperSession: (body: FundPaperCreateRequest) => request<FundPaperSession>(
    '/api/fund-paper/sessions', { method: 'POST', body: JSON.stringify(body) },
  ),
  getFundPaperSession: (sessionId: string) => request<FundPaperSession>(
    `/api/fund-paper/sessions/${encodeURIComponent(sessionId)}`,
  ),
  stopFundPaperSession: (sessionId: string) => request<FundPaperSession>(
    `/api/fund-paper/sessions/${encodeURIComponent(sessionId)}/stop`, { method: 'POST' },
  ),
  deleteFundPaperSession: (sessionId: string) => request<{ deleted: boolean }>(
    `/api/fund-paper/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' },
  ),
}
