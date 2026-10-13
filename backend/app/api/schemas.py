"""前后端稳定协议模型，与 AKShare/SQL Server 原始字段隔离。"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QuoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    stale: bool


class BarResponse(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


class IntradayBarResponse(BaseModel):
    trade_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


class WatchlistItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    name: str


class WatchlistAddRequest(BaseModel):
    symbol: str = Field(min_length=6, max_length=8)
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        """去除名称两侧空白，避免 SQL Server 中存入看不见的差异。"""

        value = value.strip()
        if not value:
            raise ValueError("股票名称不能为空")
        return value


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=6, max_length=8)
    strategy_code: str = Field(default="dual_ma", min_length=1, max_length=40)
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=20)
    # 与纸面实验共用策略参数名称，落库仍使用独立数值列。
    parameters: dict[str, int | float] = Field(default_factory=dict, max_length=12)
    start_date: date
    end_date: date
    short_window: int | None = Field(default=None, ge=1, le=250)
    long_window: int | None = Field(default=None, ge=2, le=500)
    initial_capital: float | None = Field(default=None, gt=0)
    position_ratio: float = Field(default=1, gt=0, le=1)
    commission_rate: float | None = Field(default=None, ge=0, le=0.1)
    minimum_commission: float | None = Field(default=None, ge=0, le=10000)
    stamp_duty_rate: float | None = Field(default=None, ge=0, le=0.1)
    slippage_rate: float | None = Field(default=None, ge=0, lt=1)


class BacktestAccepted(BaseModel):
    run_id: str
    status: str


class PaperStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_capital: float | None = Field(default=None, gt=0)


class PaperExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_name: str = Field(min_length=1, max_length=100)
    symbols: list[str] = Field(min_length=1, max_length=20)
    strategy_code: str = Field(min_length=1, max_length=40)
    initial_capital: float = Field(gt=0)
    # 参数以 API 字段传递，落库时由策略映射到已有的独立数值列。
    parameters: dict[str, int | float] = Field(default_factory=dict, max_length=12)

    @field_validator("task_name")
    @classmethod
    def normalize_task_name(cls, value: str) -> str:
        """去除名称首尾空白，并拒绝只输入空格的任务。"""

        name = value.strip()
        if not name:
            raise ValueError("任务名称不能为空")
        return name

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, symbols: list[str]) -> list[str]:
        """限制 A 股代码格式和同一任务内的重复股票。"""

        if len(set(symbols)) != len(symbols):
            raise ValueError("股票代码不能重复")
        if any(len(symbol) != 6 or not symbol.isdigit() for symbol in symbols):
            raise ValueError("股票代码必须是六位数字")
        return symbols


class PaperSessionAccepted(BaseModel):
    session_id: str
    status: str
