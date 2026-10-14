"""趋势过滤、区间突破与波动止损等纯策略计算。"""

from dataclasses import dataclass
from datetime import date
from math import isfinite, sqrt

from app.models import Bar


_STRATEGIES: tuple[dict[str, object], ...] = (
    {
        "code": "defensive_mean_reversion", "name": "防御型均值回归",
        "description": "长期趋势有效时交易短期超跌，按波动率控制仓位并设置 ATR 与持有期退出。",
        "ma_period": 60, "breakout_period": 20, "atr_period": 14, "atr_multiplier": 2.0,
        "short_ma_period": 5, "long_ma_period": 20, "trend_ma_period": 120,
        "max_holding_days": 5, "cooldown_days": 3, "target_annual_volatility": 0.15,
        "max_position_ratio": 0.70,
        "parameters": [
            {"key": "trend_ma_period", "label": "长期趋势均线", "type": "integer", "default": 120, "min": 21, "max": 250, "step": 1, "unit": "日", "description": "收盘价须高于该均线，且均线不得较 20 个交易日前走弱。"},
            {"key": "atr_multiplier", "label": "ATR 止损倍数", "type": "number", "default": 2.0, "min": 0.1, "max": 10.0, "step": 0.1, "unit": "倍", "description": "收盘跌破入场价减去入场 ATR 乘数时退出。"},
            {"key": "max_holding_days", "label": "最大持有期", "type": "integer", "default": 5, "min": 1, "max": 120, "step": 1, "unit": "交易日", "description": "持仓达到该交易日数仍未回归时退出。"},
            {"key": "cooldown_days", "label": "卖出后冷却期", "type": "integer", "default": 3, "min": 0, "max": 120, "step": 1, "unit": "交易日", "description": "卖出后完整等待的交易日数。"},
            {"key": "target_annual_volatility", "label": "目标年化波动率", "type": "number", "default": 0.15, "min": 0.01, "max": 2.0, "step": 0.01, "unit": "比例", "description": "小数比例，例如 0.15 表示 15%。"},
            {"key": "max_position_ratio", "label": "最大仓位", "type": "number", "default": 0.70, "min": 0.01, "max": 1.0, "step": 0.01, "unit": "比例", "description": "小数比例，例如 0.70 表示 70%。"},
        ],
    },
    {
        "code": "breakout_ma_atr",
        "name": "趋势突破 + ATR 止损",
        "description": "站上趋势均线并突破区间高点入场，使用 ATR 跟踪止损。",
        "ma_period": 60,
        "breakout_period": 20,
        "atr_period": 20,
        "atr_multiplier": 2.0,
        "short_ma_period": 5,
        "long_ma_period": 20,
        "parameters": [
            {"key": "ma_period", "label": "趋势均线周期", "type": "integer", "default": 60, "min": 1, "max": 120, "step": 1, "unit": "日", "description": "判断价格所处趋势的均线窗口。"},
            {"key": "breakout_period", "label": "突破观察周期", "type": "integer", "default": 20, "min": 1, "max": 120, "step": 1, "unit": "日", "description": "使用最近已收盘交易日的最高价作为突破线。"},
            {"key": "atr_period", "label": "ATR 计算周期", "type": "integer", "default": 20, "min": 1, "max": 100, "step": 1, "unit": "日", "description": "计算平均真实波幅的窗口。"},
            {"key": "atr_multiplier", "label": "ATR 止损倍数", "type": "number", "default": 2.0, "min": 0.1, "max": 10.0, "step": 0.1, "unit": "倍", "description": "跟踪止损距离为 ATR 乘以该倍数。"},
        ],
    },
    {
        "code": "dual_ma",
        "name": "5/20 日均线交叉",
        "description": "短均线上穿长均线买入；短均线低于长均线时卖出，等待 T+1 时仍保留退出信号。",
        "ma_period": 60,
        "breakout_period": 20,
        "atr_period": 20,
        "atr_multiplier": 2.0,
        "short_ma_period": 5,
        "long_ma_period": 20,
        "parameters": [
            {"key": "short_ma_period", "label": "短均线周期", "type": "integer", "default": 5, "min": 1, "max": 120, "step": 1, "unit": "日", "description": "短周期均线向上穿越长均线时产生买入信号。"},
            {"key": "long_ma_period", "label": "长均线周期", "type": "integer", "default": 20, "min": 2, "max": 120, "step": 1, "unit": "日", "description": "必须大于短均线周期，用于确认中期趋势。"},
        ],
    },
    {
        "code": "momentum_rotation",
        "name": "20 日强弱轮动（组合策略）",
        "description": "按 20 日涨幅排序，只持有站上 60 日均线的强势股，每周等权调仓，最多持有 3 只。",
        "ma_period": 60,
        "breakout_period": 20,
        "atr_period": 20,
        "atr_multiplier": 2.0,
        "short_ma_period": 5,
        "long_ma_period": 20,
        "parameters": [
            {"key": "momentum_period_days", "label": "动量周期", "type": "integer", "default": 20, "min": 1, "max": 120, "step": 1, "unit": "日", "description": "按最近完整交易日收盘价计算相对强弱。"},
            {"key": "trend_ma_period", "label": "趋势均线周期", "type": "integer", "default": 60, "min": 2, "max": 120, "step": 1, "unit": "日", "description": "仅选择收盘价高于该周期简单均线的股票。"},
            {"key": "max_positions", "label": "最多持仓数", "type": "integer", "default": 3, "min": 1, "max": 20, "step": 1, "unit": "只", "description": "按动量排名选择合格股票，目标仓位等权分配。"},
            {"key": "rebalance_interval_days", "label": "调仓间隔", "type": "integer", "default": 7, "min": 1, "max": 30, "step": 1, "unit": "日", "description": "两次组合调仓之间至少间隔的日历天数。"},
        ],
    },
)


@dataclass(frozen=True)
class DefensiveDecision:
    """防御策略在信号日收盘后生成的决策，不包含盘中成交报价。"""
    action: str | None
    reason: str
    entry_atr: float | None = None
    position_ratio: float | None = None


def _rsi(closes: list[float], period: int = 2) -> float:
    """按固定窗口平均涨跌幅计算 RSI，避免嵌套条件表达式掩盖边界。"""
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
    gains = sum(max(change, 0) for change in changes) / period
    losses = sum(max(-change, 0) for change in changes) / period
    if losses == 0:
        return 50.0 if gains == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def evaluate_defensive_mean_reversion(
    signal_bars: list[Bar], entry_date: date | None, entry_price: float | None,
    entry_atr: float | None, last_exit_date: date | None, trend_ma_period: int,
    atr_period: int, atr_multiplier: float, max_holding_days: int, cooldown_days: int,
    target_annual_volatility: float, max_position_ratio: float,
) -> DefensiveDecision:
    """仅以完整收盘日线和持仓状态计算防御策略决策。"""
    if (trend_ma_period <= 20 or atr_period <= 0 or max_holding_days <= 0 or cooldown_days < 0
            or any(not isfinite(v) or v <= 0 for v in (atr_multiplier, target_annual_volatility, max_position_ratio))
            or max_position_ratio > 1 or not signal_bars):
        return DefensiveDecision(None, "invalid_parameters_or_history")
    if any(not all(isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
           or b.high < max(b.open, b.close) or b.low > min(b.open, b.close) for b in signal_bars):
        return DefensiveDecision(None, "invalid_daily_bars")
    if len({bar.trade_date for bar in signal_bars}) != len(signal_bars):
        return DefensiveDecision(None, "duplicate_trade_dates")
    bars = sorted(signal_bars, key=lambda bar: bar.trade_date)
    if len(bars) < max(trend_ma_period + 20, 21, atr_period + 1):
        return DefensiveDecision(None, "insufficient_history")
    closes = [bar.close for bar in bars]
    close = closes[-1]
    trend = sum(closes[-trend_ma_period:]) / trend_ma_period
    prior = sum(closes[-trend_ma_period-20:-20]) / trend_ma_period
    trend_ok = close > trend and trend >= prior
    # ATR 采用原始 OHLC 的简单平均真实波幅，包含相邻交易日跳空。
    trs = [max(bars[i].high-bars[i].low, abs(bars[i].high-closes[i-1]),
               abs(bars[i].low-closes[i-1])) for i in range(len(bars)-atr_period, len(bars))]
    current_atr = sum(trs) / atr_period
    if not isfinite(current_atr) or current_atr <= 0:
        return DefensiveDecision(None, "invalid_atr")

    if entry_date is not None:
        if (entry_price is None or entry_atr is None or not isfinite(entry_price)
                or not isfinite(entry_atr) or entry_price <= 0 or entry_atr <= 0):
            return DefensiveDecision(None, "invalid_position")
        rsi2 = _rsi(closes)
        held = sum(entry_date < bar.trade_date <= bars[-1].trade_date for bar in bars)
        if not trend_ok:
            return DefensiveDecision("sell", "trend_filter_failed")
        if close <= entry_price - atr_multiplier * entry_atr:
            return DefensiveDecision("sell", "atr_stop")
        if held >= max_holding_days:
            return DefensiveDecision("sell", "maximum_holding_period")
        if close >= sum(closes[-5:])/5 or rsi2 >= 60:
            return DefensiveDecision("sell", "mean_reversion")
        return DefensiveDecision(None, "hold")

    if last_exit_date is not None:
        exit_index = next((i for i in range(len(bars)-1, -1, -1) if bars[i].trade_date == last_exit_date), None)
        if exit_index is None or len(bars)-1-exit_index <= cooldown_days:
            return DefensiveDecision(None, "cooldown")
    if not trend_ok:
        return DefensiveDecision(None, "trend_filter_failed")
    rsi2 = _rsi(closes)
    window = closes[-20:]
    mean = sum(window)/20
    lower = mean - 2*sqrt(sum((v-mean)**2 for v in window)/20)
    if close >= lower or rsi2 > 10:
        return DefensiveDecision(None, "no_oversold_signal")
    returns = [closes[i]/closes[i-1]-1 for i in range(len(closes)-20, len(closes))]
    average = sum(returns)/20
    volatility = sqrt(sum((v-average)**2 for v in returns)/20)*sqrt(252)
    if not isfinite(volatility) or volatility <= 0:
        return DefensiveDecision(None, "invalid_volatility")
    return DefensiveDecision("buy", "oversold_mean_reversion", current_atr,
                             min(max_position_ratio, target_annual_volatility/volatility))


def get_strategy_catalog() -> list[dict[str, object]]:
    """返回策略目录副本；任务创建时会把周期逐列保存到 SQL Server。"""

    # 参数定义使用嵌套字典；逐项复制可避免一次请求改写全局策略默认值。
    return [
        {**strategy, "parameters": [dict(parameter) for parameter in strategy.get("parameters", [])]}
        for strategy in _STRATEGIES
    ]


def rank_momentum_rotation(
    bars_by_symbol: dict[str, list[Bar]],
    momentum_period_days: int,
    trend_ma_period: int,
    max_positions: int,
) -> list[str] | None:
    """按完整日线的动量排序；缺少任一股票历史时不生成部分组合。"""

    if min(momentum_period_days, trend_ma_period, max_positions) <= 0:
        raise ValueError("轮动策略周期和持仓数必须大于 0")

    required = max(momentum_period_days + 1, trend_ma_period)
    ranked: list[tuple[float, str]] = []
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < required:
            return None
        closes = [bar.close for bar in bars[-required:]]
        if any(close <= 0 for close in closes):
            return None

        momentum = closes[-1] / closes[-momentum_period_days - 1] - 1
        trend_average = sum(closes[-trend_ma_period:]) / trend_ma_period
        if momentum > 0 and closes[-1] > trend_average:
            ranked.append((momentum, symbol))

    # 同一收益率按股票代码排序，保证重复计算目标组合时结果稳定。
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [symbol for _, symbol in ranked[:max_positions]]


def find_entry_atr(
    bars: list[Bar],
    quote_price: float,
    ma_period: int = 60,
    breakout_period: int = 20,
    atr_period: int = 20,
) -> float | None:
    """价格突破前 N 日高点且高于趋势均线时返回 ATR，否则不入场。"""

    if min(ma_period, breakout_period, atr_period) <= 0:
        raise ValueError("策略周期必须大于 0")
    required = max(ma_period, breakout_period, atr_period + 1)
    if len(bars) < required or quote_price <= 0:
        return None

    # 日线由调用方排除当前交易日，避免把未收盘 K 线混入突破参考值。
    trend_average = sum(bar.close for bar in bars[-ma_period:]) / ma_period
    breakout_high = max(bar.high for bar in bars[-breakout_period:])
    if quote_price <= breakout_high or quote_price <= trend_average:
        return None

    # ATR 使用真实波幅，跳空也会反映到止损宽度中。
    ranges = []
    for index in range(len(bars) - atr_period, len(bars)):
        bar = bars[index]
        previous_close = bars[index - 1].close
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
    return sum(ranges) / atr_period


def find_dual_ma_signal(
    bars: list[Bar],
    quote_price: float,
    short_window: int = 5,
    long_window: int = 20,
) -> str | None:
    """用已收盘日线与当前报价检测上穿；看跌关系持续给出可执行的退出信号。"""

    if short_window <= 0 or long_window <= short_window:
        raise ValueError("双均线周期必须为正，且短周期小于长周期")
    if quote_price <= 0 or len(bars) < long_window:
        return None

    closes = [bar.close for bar in bars]
    previous_short = sum(closes[-short_window:]) / short_window
    previous_long = sum(closes[-long_window:]) / long_window
    # 周期为 1 时没有已收盘参考价，不能让 Python 的 [0:] 切片误取整段历史。
    prior_short_sum = sum(closes[-(short_window - 1):]) if short_window > 1 else 0
    current_short = (prior_short_sum + quote_price) / short_window
    current_long = (sum(closes[-long_window + 1:]) + quote_price) / long_window

    if previous_short <= previous_long and current_short > current_long:
        return "buy"
    # 保持看跌信号，确保买入当日受 T+1 限制时，次日仍有机会退出。
    if current_short < current_long:
        return "sell"
    return None


def trailing_stop(highest_price: float, atr: float, multiplier: float = 2.0) -> float:
    """用入场 ATR 和持仓后的最高成交价计算跟踪止损价。"""

    if highest_price <= 0 or atr < 0 or multiplier <= 0:
        raise ValueError("止损价格、ATR 或倍数无效")
    return max(0.0, highest_price - multiplier * atr)


def should_exit(price: float, highest_price: float, atr: float, multiplier: float = 2.0) -> bool:
    """当前有效行情跌破跟踪止损时退出多头持仓。"""

    return price > 0 and price <= trailing_stop(highest_price, atr, multiplier)
