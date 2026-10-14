/** 前端与 FastAPI 使用的稳定字段合同，名称保持与 API 响应一致。 */
export interface AppConfig {
  refresh_interval_seconds: number
  chart_refresh_interval_seconds: number
  intraday_refresh_interval_seconds: number
  paper_trading: {
    symbols: string[]
    ma_period: number
    breakout_period: number
    atr_period: number
    atr_multiplier: number
  }
  fund: {
    exchange_refresh_interval_seconds: number
    open_nav_refresh_interval_seconds: number
    worker_interval_seconds: number
    default_initial_capital: number
    commission_rate: number
    minimum_commission: number
    stamp_duty_rate: number
    slippage_rate: number
    subscription_fee_rate: number
    redemption_fee_rate: number
  }
  stock_screen: {
    default_universe_size: number
    max_universe_size: number
    candidate_count: number
  }
  defaults: {
    initial_capital: number
    short_window: number
    long_window: number
    commission_rate: number
    minimum_commission: number
    stamp_duty_rate: number
    slippage_rate: number
  }
}

/** 07 选股实验的结构化扫描结果；分数是相对排序，不是上涨概率。 */
export interface StockScreenSummary {
  run_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  signal_cutoff: string
  requested_count: number
  candidate_count: number
  rule_version: string
  created_at: string
  completed_at: string | null
  error_message: string | null
  processed_count: number
  eligible_count: number
}

export interface StockScreenOutcome {
  symbol: string
  horizon_days: 5 | 20
  outcome_date: string
  outcome_close: number
  return_rate: number
}

export interface StockScreenCandidate {
  symbol: string
  name: string
  snapshot_turnover: number
  status: string
  signal_date: string
  signal_close: number
  momentum_score: number | null
  recent_5_return: number | null
  reversal_score: number | null
  momentum_rank: number | null
  reversal_rank: number | null
  outcomes: StockScreenOutcome[]
}

export interface StockScreenMetric {
  strategy_code: 'momentum_trend' | 'relative_reversal'
  horizon_days: 5 | 20
  mean_return: number | null
  benchmark_return: number | null
  win_rate: number | null
  sample_count: number
  benchmark_count: number
}

export interface StockScreenDetail extends StockScreenSummary {
  excluded_count: number
  candidates: StockScreenCandidate[]
  metrics: StockScreenMetric[]
}

export interface IntradayBar {
  trade_time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  turnover: number
}

export type FundType = 'etf' | 'lof' | 'open_end'

export interface FundInstrument {
  fund_type: FundType
  symbol: string
  name: string
  subscription_status: string
  redemption_status: string
  fee_description: string
  category: string
}

export interface FundQuote {
  fund_type: FundType
  symbol: string
  name: string
  latest_price: number
  change_percent: number
  open: number | null
  high: number | null
  low: number | null
  previous_close: number | null
  volume: number
  turnover: number
  source: string
  fetched_at: string
  nav_date: string | null
  accumulated_nav: number | null
  subscription_status: string
  redemption_status: string
  fee_description: string
  stale: boolean
  category: string
}

export interface FundBar extends Bar {}
export interface FundIntradayBar extends IntradayBar {}

export interface FundNavPoint {
  trade_date: string
  unit_nav: number
  accumulated_nav: number | null
  daily_return: number | null
}

export interface FundPaperFund {
  symbol: string
  name: string
  shares: number
  entry_date: string | null
  entry_price: number | null
  cost_basis: number
  last_price: number | null
  last_quote_at: string | null
}

export interface FundPaperOrder {
  order_id?: number
  idempotency_key?: string
  symbol: string
  fund_type: FundType
  side: 'buy' | 'sell' | 'dividend' | string
  order_mode: 'market' | 'nav_next' | 'cash_event' | string
  status: 'pending' | 'filled' | 'cancelled' | string
  signal_date: string
  execution_date: string | null
  amount: number
  quantity: number
  execution_price: number | null
  fee: number
  reason: string
}

export interface FundPaperTrade {
  symbol: string
  trade_date: string
  signal_date: string
  side: 'buy' | 'sell' | 'dividend' | string
  execution_price: number
  quantity: number
  notional: number
  fee: number
  reason: string
}

export interface FundPaperSessionSummary {
  session_id: string
  task_name: string
  fund_type: FundType
  strategy_code: string
  status: 'running' | 'completed' | 'failed' | 'stopped' | string
  started_at: string
  completed_at: string | null
  initial_capital: number
  cash: number
  market_value: number
  equity: number
  total_return: number
  max_drawdown: number
  error_message: string | null
}

export interface FundPaperEquityPoint extends PaperEquityPoint {}

export interface FundPaperSession extends FundPaperSessionSummary {
  commission_rate: number
  minimum_commission: number
  stamp_duty_rate: number
  slippage_rate: number
  subscription_fee_rate: number
  redemption_fee_rate: number
  ma_period: number
  breakout_period: number
  atr_period: number
  atr_multiplier: number
  short_ma_period: number
  long_ma_period: number
  momentum_period_days: number
  trend_ma_period: number
  max_positions: number
  rebalance_interval_days: number
  risk_lookback_days: number
  last_rebalanced_at: string | null
  error_message: string | null
  funds: FundPaperFund[]
  orders: FundPaperOrder[]
  trades: FundPaperTrade[]
  equity_curve: FundPaperEquityPoint[]
}

export interface FundPaperCreateRequest {
  task_name: string
  fund_type: FundType
  symbols: string[]
  strategy_code: string
  initial_capital: number
  parameters: Partial<Record<FundPaperStrategyParameterKey, number>>
}

export interface PaperSymbol {
  symbol: string
  name: string
  budget: number
  shares: number
  entry_date: string | null
  entry_price: number | null
  cost_basis: number
  highest_price: number | null
  atr_value: number | null
  last_price: number | null
  last_quote_at: string | null
  // 防御策略的冷却起点与去重日期按股票保存，旧账本允许为空。
  last_exit_date: string | null
  last_decision_date: string | null
}

export interface PaperEquityPoint {
  trade_date: string
  cash: number
  market_value: number
  equity: number
  drawdown: number
}

export interface PaperTrade {
  symbol: string
  trade_date: string
  // 历史纸面成交可能没有保存信号日期；新成交以收盘信号日和报价执行日分开审计。
  signal_date: string | null
  side: 'buy' | 'sell' | string
  price: number
  quantity: number
  notional: number
  commission: number
  stamp_duty: number
  reason: string
}

export type PaperStrategyParameterKey =
  | 'ma_period'
  | 'breakout_period'
  | 'atr_period'
  | 'atr_multiplier'
  | 'short_ma_period'
  | 'long_ma_period'
  // 组合轮动参数随纸面策略目录返回，并独立保存到任务记录。
  | 'momentum_period_days'
  | 'trend_ma_period'
  | 'max_positions'
  | 'rebalance_interval_days'
  | 'max_holding_days'
  | 'cooldown_days'
  | 'target_annual_volatility'
  | 'max_position_ratio'

// 波动率观察期仅属于基金任务，避免该参数扩散到股票回测 DTO。
export type FundPaperStrategyParameterKey = PaperStrategyParameterKey | 'risk_lookback_days'

export interface PaperStrategyParameter {
  key: PaperStrategyParameterKey
  label: string
  type: 'integer' | 'number'
  default: number
  min: number
  max: number
  step: number
  unit: string
  description: string
}

export interface PaperStrategy {
  code: string
  name: string
  description: string
  ma_period: number
  breakout_period: number
  atr_period: number
  atr_multiplier: number
  short_ma_period: number
  long_ma_period: number
  max_holding_days?: number
  cooldown_days?: number
  target_annual_volatility?: number
  max_position_ratio?: number
  parameters: PaperStrategyParameter[]
}

export interface FundPaperStrategyParameter extends Omit<PaperStrategyParameter, 'key'> {
  key: FundPaperStrategyParameterKey
}

export interface FundPaperStrategy extends Omit<PaperStrategy, 'parameters'> {
  available_for_new_tasks?: boolean
  parameters: FundPaperStrategyParameter[]
}

export interface PaperSessionSummary {
  session_id: string
  task_name: string
  strategy_code: string
  status: 'running' | 'completed' | 'failed' | 'stopped' | string
  started_at: string
  ends_at: string | null
  completed_at: string | null
  initial_capital: number
  cash: number
  market_value: number
  equity: number
  total_return: number
  max_drawdown: number
  symbol_count: number
  error_message: string | null
}

export interface PaperTaskCreateRequest {
  task_name: string
  symbols: string[]
  strategy_code: string
  initial_capital: number
  parameters: Partial<Record<PaperStrategyParameterKey, number>>
}

export interface PaperSession {
  session_id: string
  task_name: string
  strategy_code: string
  status: 'running' | 'completed' | 'failed' | 'stopped' | string
  started_at: string
  ends_at: string | null
  completed_at: string | null
  initial_capital: number
  cash: number
  market_value: number
  equity: number
  total_return: number
  max_drawdown: number
  ma_period: number
  breakout_period: number
  atr_period: number
  atr_multiplier: number
  short_ma_period: number
  long_ma_period: number
  momentum_period_days: number
  trend_ma_period: number
  max_positions: number
  rebalance_interval_days: number
  last_rebalanced_at: string | null
  max_holding_days: number
  cooldown_days: number
  target_annual_volatility: number
  max_position_ratio: number
  commission_rate: number
  minimum_commission: number
  stamp_duty_rate: number
  slippage_rate: number
  error_message: string | null
  symbols: PaperSymbol[]
  equity_curve: PaperEquityPoint[]
  trades: PaperTrade[]
}

export interface Quote {
  symbol: string
  name: string
  latest_price: number
  change_amount: number
  change_percent: number
  open: number
  high: number
  low: number
  previous_close: number
  volume_hands: number
  turnover: number
  source: string
  fetched_at: string
  stale: boolean
}

export interface WatchedStock {
  symbol: string
  name: string
}

export interface Bar {
  trade_date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  turnover: number
}

export interface BacktestRequest {
  symbol: string
  strategy_code: string
  symbols: string[]
  parameters: Partial<Record<PaperStrategyParameterKey, number>>
  start_date: string
  end_date: string
  short_window: number
  long_window: number
  initial_capital: number
  position_ratio: number
  commission_rate: number
  minimum_commission: number
  stamp_duty_rate: number
  slippage_rate: number
}

export interface BacktestSummary {
  run_id: string
  symbol: string
  start_date: string
  end_date: string
  strategy_code: string
  symbol_count?: number
  short_window: number
  long_window: number
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | string
  progress: number
  total_return: number | null
  annualized_return: number | null
  max_drawdown: number | null
  created_at: string
  error_message?: string | null
}

export interface EquityPoint {
  trade_date: string
  cash: number
  shares: number
  market_value: number
  equity: number
  drawdown: number
  benchmark_equity: number
}

export interface Trade {
  symbol?: string
  trade_date: string
  signal_date: string
  side: 'buy' | 'sell' | string
  price: number
  quantity: number
  notional: number
  commission: number
  stamp_duty: number
  reason: string
}

export interface BacktestRecord extends BacktestSummary {
  symbols: string[]
  ma_period: number
  breakout_period: number
  atr_period: number
  atr_multiplier: number
  momentum_period_days: number
  trend_ma_period: number
  max_positions: number
  rebalance_interval_days: number
  max_holding_days: number
  cooldown_days: number
  target_annual_volatility: number
  initial_capital: number
  // 防御策略的 max_position_ratio 落入既有 position_ratio 响应列。
  position_ratio: number
  commission_rate: number
  minimum_commission: number
  stamp_duty_rate: number
  slippage_rate: number
  annualized_volatility: number | null
  sharpe_ratio: number | null
  win_rate: number | null
  closed_trade_count: number
  fees_total: number
  dividends_total: number
  benchmark_return: number | null
  equity_curve: EquityPoint[]
  trades: Trade[]
  warnings: string[]
}

export interface BacktestList {
  items: BacktestSummary[]
  total: number
  offset: number
  limit: number
}
