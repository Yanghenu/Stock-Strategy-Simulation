"""基金行情 API 的请求与响应 DTO。"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


FundTypeValue = Literal["etf", "lof", "open_end"]


class FundInstrumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fund_type: FundTypeValue
    symbol: str
    name: str
    subscription_status: str = "未知"
    redemption_status: str = "未知"
    fee_description: str = ""
    category: str = ""


class FundQuoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fund_type: FundTypeValue
    symbol: str
    name: str
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


class FundBarResponse(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


class FundIntradayResponse(BaseModel):
    trade_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


class FundNavResponse(BaseModel):
    trade_date: date
    unit_nav: float
    accumulated_nav: float | None
    daily_return: float | None


