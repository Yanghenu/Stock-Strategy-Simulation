"""基金行情与实验间共享的结构化领域类型。"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

FundType = Literal["etf", "lof", "open_end"]


class FundDataError(ValueError):
    """基金代码、行情字段或交易规则不符合预期。"""


@dataclass(frozen=True)
class FundInstrument:
    symbol: str
    name: str
    fund_type: FundType
    subscription_status: str = "未知"
    redemption_status: str = "未知"
    fee_description: str = ""
    category: str = ""


@dataclass(frozen=True)
class FundQuote:
    symbol: str
    name: str
    fund_type: FundType
    latest_price: float
    change_percent: float
    open: float | None
    high: float | None
    low: float | None
    previous_close: float | None
    volume: float
    turnover: float
    source: str
    fetched_at: datetime
    nav_date: date | None = None
    accumulated_nav: float | None = None
    subscription_status: str = "未知"
    redemption_status: str = "未知"
    fee_description: str = ""
    stale: bool = False
    category: str = ""


@dataclass(frozen=True)
class FundBar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0
    turnover: float = 0


@dataclass(frozen=True)
class FundIntradayBar:
    trade_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0
    turnover: float = 0


@dataclass(frozen=True)
class FundNavPoint:
    trade_date: date
    unit_nav: float
    accumulated_nav: float | None
    daily_return: float | None


@dataclass(frozen=True)
class FundDistribution:
    ex_date: date
    cash_per_unit: float
    source_key: str
