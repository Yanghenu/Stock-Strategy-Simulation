"""趋势过滤、区间突破与波动止损等纯策略计算。"""

from app.models import Bar


_STRATEGIES: tuple[dict[str, object], ...] = (
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
