"""后端各层共享的结构化领域类型；金额和价格用数值字段，不序列化为 JSON 列。"""

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Bar:
    """标准化日线；原始 OHLC 用于撮合，前复权收盘只用于信号。"""

    trade_date: date
    open: float
    high: float
    low: float
    close: float
    adj_open: float
    adj_high: float
    adj_low: float
    adj_close: float
    volume: float = 0
    turnover: float = 0


@dataclass(frozen=True)
class IntradayBar:
    """标准化分钟行情；时间统一为上海时区，成交量统一为股。"""

    trade_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0
    turnover: float = 0


@dataclass(frozen=True)
class WatchedStock:
    """SQL Server 中持久化的一只关注标的。"""

    symbol: str
    name: str


@dataclass(frozen=True)
class CorporateAction:
    """已实施的公司权益事件；送转比例按每股新增股数表示。"""

    symbol: str
    record_date: date | None
    ex_date: date | None
    cash_per_share: float = 0
    bonus_shares_per_share: float = 0
    status: str = "implemented"
    source_key: str = ""


@dataclass(frozen=True)
class BacktestParams:
    """一次回测的完整、可审计参数；旧调用的前十一个位置参数保持兼容。"""

    symbol: str
    start_date: date
    end_date: date
    short_window: int
    long_window: int
    initial_capital: float
    position_ratio: float = 1
    commission_rate: float = 0.0003
    minimum_commission: float = 5
    stamp_duty_rate: float = 0.001
    slippage_rate: float = 0.0005
    strategy_code: str = "dual_ma"
    symbols: tuple[str, ...] = ()
    ma_period: int = 60
    breakout_period: int = 20
    atr_period: int = 20
    atr_multiplier: float = 2.0
    momentum_period_days: int = 20
    trend_ma_period: int = 60
    max_positions: int = 3
    rebalance_interval_days: int = 7


@dataclass(frozen=True)
class Trade:
    trade_date: date
    signal_date: date
    side: str
    price: float
    quantity: int
    notional: float
    commission: float
    stamp_duty: float
    reason: str
    # 老回测记录没有逐笔股票代码；新组合成交会保存具体标的。
    symbol: str | None = None

    @property
    def fees(self) -> float:
        """汇总佣金和印花税，滑点已反映在成交价格中。"""

        return self.commission + self.stamp_duty


@dataclass(frozen=True)
class EquityPoint:
    trade_date: date
    cash: float
    shares: int
    market_value: float
    equity: float
    drawdown: float
    benchmark_equity: float


@dataclass(frozen=True)
class ClosedTrade:
    entry_date: date
    exit_date: date
    quantity: int
    profit_loss: float
    return_rate: float | None
    cost_basis: float = 0


@dataclass(frozen=True)
class Metrics:
    total_return: float
    annualized_return: float | None
    max_drawdown: float
    annualized_volatility: float | None
    sharpe_ratio: float | None
    win_rate: float | None
    closed_trade_count: int


@dataclass(frozen=True)
class OpenPosition:
    entry_date: date
    shares: int
    cost_basis: float
    dividends: float


@dataclass
class BacktestResult:
    symbol: str
    metrics: Metrics
    trades: list[Trade] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    fees_total: float = 0
    dividends_total: float = 0
    benchmark_return: float = 0
    open_position: OpenPosition | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Quote:
    """实时行情快照；成交量单位沿用 AKShare 文档中的“手”。"""

    symbol: str
    name: str
    latest_price: float
    change_amount: float
    change_percent: float
    open: float
    high: float
    low: float
    previous_close: float
    volume_hands: float
    turnover: float
    source: str
    fetched_at: datetime
    stale: bool = False
