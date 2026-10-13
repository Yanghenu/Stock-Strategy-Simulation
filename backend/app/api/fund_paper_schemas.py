"""基金纸面实验 API DTO；参数以独立数值字段写入 SQL，不保存 JSON 字段。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.fund_schemas import FundTypeValue


class FundPaperCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_name: str = Field(min_length=1, max_length=100)
    fund_type: FundTypeValue
    symbols: list[str] = Field(min_length=1, max_length=20)
    strategy_code: str = Field(min_length=1, max_length=40)
    initial_capital: float = Field(gt=0, le=100_000_000_000)
    parameters: dict[str, float] = Field(default_factory=dict)


class FundPaperSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: str
    task_name: str
    fund_type: FundTypeValue
    strategy_code: str
    status: str
    started_at: Any
    completed_at: Any = None
    initial_capital: float
    cash: float
    market_value: float
    equity: float
    total_return: float
    max_drawdown: float
    commission_rate: float = 0.0003
    minimum_commission: float = 5
    stamp_duty_rate: float = 0
    slippage_rate: float = 0
    subscription_fee_rate: float = 0.0015
    redemption_fee_rate: float = 0.005
    # 给详情页返回实际任务参数；列表接口缺少的列使用兼容默认值。
    ma_period: int = 60
    breakout_period: int = 20
    atr_period: int = 20
    atr_multiplier: float = 2.0
    short_ma_period: int = 5
    long_ma_period: int = 20
    momentum_period_days: int = 20
    trend_ma_period: int = 60
    max_positions: int = 3
    rebalance_interval_days: int = 7
    risk_lookback_days: int = 60
    last_rebalanced_at: Any = None
    error_message: str | None = None
    funds: list[dict[str, Any]] = Field(default_factory=list)
    orders: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)
