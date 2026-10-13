"""基金实验的策略信号和交易成本计算。"""

from math import floor, isfinite, sqrt
from typing import Any

from app.fund_data.models import FundBar
from app.paper_trading.strategy import find_dual_ma_signal, find_entry_atr


_FUND_STRATEGIES: tuple[dict[str, Any], ...] = (
    {
        "code": "fund_equal_rebalance",
        "name": "基金等权定期再平衡",
        "description": "初始将资金等分到所选基金，按设定间隔恢复等权；开放式基金按下一次公布净值成交。",
        "ma_period": 60, "breakout_period": 20, "atr_period": 20, "atr_multiplier": 2.0,
        "short_ma_period": 5, "long_ma_period": 20,
        "parameters": [
            {"key": "rebalance_interval_days", "label": "调仓间隔", "type": "integer", "default": 7,
             "min": 1, "max": 30, "step": 1, "unit": "日", "description": "两次组合调仓之间至少间隔的日历天数。"},
        ],
    },
    {
        "code": "fund_low_volatility",
        "name": "低波动风险配置",
        "description": "根据历史日收益波动率反比分配仓位，波动较低的基金获得较高权重；历史不足时退回等权。",
        "ma_period": 60, "breakout_period": 20, "atr_period": 20, "atr_multiplier": 2.0,
        "short_ma_period": 5, "long_ma_period": 20,
        "parameters": [
            {"key": "risk_lookback_days", "label": "波动率观察期", "type": "integer", "default": 60,
             "min": 20, "max": 120, "step": 1, "unit": "日", "description": "用最近完整行情计算日收益波动率并反比分配权重。"},
            {"key": "rebalance_interval_days", "label": "调仓间隔", "type": "integer", "default": 7,
             "min": 1, "max": 30, "step": 1, "unit": "日", "description": "两次组合调仓之间至少间隔的日历天数。"},
        ],
    },
    {
        "code": "fund_relative_strength",
        "name": "基金相对强弱轮动",
        "description": "按近期收益筛选正动量且高于趋势均线的基金，最多持有指定数量并定期等权调仓；没有合格基金时持有现金。",
        "ma_period": 60, "breakout_period": 20, "atr_period": 20, "atr_multiplier": 2.0,
        "short_ma_period": 5, "long_ma_period": 20,
        "parameters": [
            {"key": "momentum_period_days", "label": "动量周期", "type": "integer", "default": 20,
             "min": 1, "max": 120, "step": 1, "unit": "日", "description": "按最近完整行情计算累计收益并排序。"},
            {"key": "trend_ma_period", "label": "趋势均线周期", "type": "integer", "default": 60,
             "min": 2, "max": 120, "step": 1, "unit": "日", "description": "只选择当前价格高于该周期简单均线的基金。"},
            {"key": "max_positions", "label": "最多持仓数", "type": "integer", "default": 3,
             "min": 1, "max": 20, "step": 1, "unit": "只", "description": "按相对强弱排名选择合格基金并等权配置。"},
            {"key": "rebalance_interval_days", "label": "调仓间隔", "type": "integer", "default": 7,
             "min": 1, "max": 30, "step": 1, "unit": "日", "description": "两次组合调仓之间至少间隔的日历天数。"},
        ],
    },
)

FUND_PORTFOLIO_STRATEGY_CODES = frozenset(str(item["code"]) for item in _FUND_STRATEGIES)


def get_fund_strategy_catalog() -> list[dict[str, Any]]:
    """返回独立的基金组合策略目录，避免复用股票策略默认值。"""

    return [
        {**strategy, "available_for_new_tasks": True,
         "parameters": [dict(parameter) for parameter in strategy["parameters"]]}
        for strategy in _FUND_STRATEGIES
    ]


def portfolio_target_weights(
    strategy_code: str,
    bars_by_symbol: dict[str, list[FundBar]],
    parameters: dict[str, Any],
) -> dict[str, float] | None:
    """按基金历史净值/价格计算目标权重；None 表示相对强弱历史尚不足。"""

    symbols = sorted(bars_by_symbol)
    if not symbols:
        return {}
    if strategy_code == "fund_equal_rebalance":
        return {symbol: 1 / len(symbols) for symbol in symbols}
    if strategy_code == "fund_low_volatility":
        window = int(parameters.get("risk_lookback_days", 60))
        if window <= 0:
            raise ValueError("波动率观察期必须大于 0")
        volatilities: dict[str, float] = {}
        for symbol in symbols:
            closes = [bar.close for bar in bars_by_symbol[symbol][-window - 1:]]
            if len(closes) < window + 1 or any(not isfinite(value) or value <= 0 for value in closes):
                return {item: 1 / len(symbols) for item in symbols}
            returns = [current / previous - 1 for previous, current in zip(closes, closes[1:])]
            average = sum(returns) / len(returns)
            volatility = sqrt(sum((value - average) ** 2 for value in returns) / len(returns))
            if not isfinite(volatility) or volatility <= 1e-12:
                return {item: 1 / len(symbols) for item in symbols}
            volatilities[symbol] = volatility
        # 反波动率归一化让波动较大的基金自动降低目标仓位。
        inverse = {symbol: 1 / volatilities[symbol] for symbol in symbols}
        total = sum(inverse.values())
        return {symbol: weight / total for symbol, weight in inverse.items()}
    if strategy_code == "fund_relative_strength":
        momentum_days = int(parameters.get("momentum_period_days", 20))
        trend_days = int(parameters.get("trend_ma_period", 60))
        max_positions = int(parameters.get("max_positions", 3))
        if min(momentum_days, trend_days, max_positions) <= 0:
            raise ValueError("基金轮动周期和持仓数量必须大于 0")
        required = max(momentum_days + 1, trend_days)
        ranked: list[tuple[float, str]] = []
        for symbol in symbols:
            closes = [bar.close for bar in bars_by_symbol[symbol][-required:]]
            if len(closes) < required:
                return None
            if any(not isfinite(value) or value <= 0 for value in closes):
                return None
            momentum = closes[-1] / closes[-momentum_days - 1] - 1
            trend_average = sum(closes[-trend_days:]) / trend_days
            if momentum > 0 and closes[-1] > trend_average:
                ranked.append((momentum, symbol))
        # 排名相同时按代码稳定排序，保证相同历史数据重复计算目标组合一致。
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected = [symbol for _, symbol in ranked[:max_positions]]
        return {symbol: 1 / len(selected) for symbol in selected} if selected else {}
    raise ValueError("不支持的基金组合策略")


def strategy_signal(strategy_code: str, bars: list[FundBar], price: float, parameters: dict[str, Any]) -> dict[str, Any]:
    """复用股票实验已验证的信号函数，日线输入排除正在形成的当日数据。"""

    if strategy_code == "dual_ma":
        side = find_dual_ma_signal(bars, price, int(parameters["short_ma_period"]), int(parameters["long_ma_period"]))
        return {"side": side, "atr": None, "reason": "双均线交叉"}
    if strategy_code == "breakout_ma_atr":
        atr = find_entry_atr(bars, price, int(parameters["ma_period"]), int(parameters["breakout_period"]), int(parameters["atr_period"]))
        return {"side": "buy" if atr is not None else None, "atr": atr, "reason": "趋势突破 + ATR 止损"}
    if strategy_code == "momentum_rotation":
        return {"side": None, "atr": None, "reason": "组合轮动策略按组合信号调仓"}
    raise ValueError("不支持的基金实验策略")


def execute_exchange_trade(
    side: str,
    price: float,
    *,
    cash: float,
    budget: float,
    shares: float,
    commission_rate: float,
    minimum_commission: float,
    stamp_duty_rate: float,
    slippage_rate: float,
    quantity: float | None = None,
) -> tuple[float, float, dict[str, Any] | None]:
    """按 100 份整数手计算 ETF/LOF 成交、滑点和费率。"""

    if side not in {"buy", "sell"} or price <= 0:
        raise ValueError("交易方向或场内价格无效")
    execution_price = price * (1 + slippage_rate if side == "buy" else 1 - slippage_rate)
    if execution_price <= 0:
        raise ValueError("滑点后的场内价格无效")
    if side == "buy":
        available = min(max(0.0, cash), max(0.0, budget))
        lot_cost = execution_price * 100
        lots = floor(available / max(0.00000001, lot_cost * (1 + commission_rate)))
        quantity = lots * 100
        notional = quantity * execution_price
        fee = max(minimum_commission, notional * commission_rate) if quantity else 0.0
        if notional + fee > cash + 0.00001:
            quantity = max(0, quantity - 100)
            notional = quantity * execution_price
            fee = max(minimum_commission, notional * commission_rate) if quantity else 0.0
        if quantity <= 0:
            return cash, shares, None
        return cash - notional - fee, shares + quantity, {
            "side": side, "quantity": quantity, "execution_price": execution_price,
            "notional": notional, "fee": fee, "commission": fee, "amount": notional + fee,
        }
    sell_quantity = min(max(0.0, shares), max(0.0, quantity)) if quantity is not None else max(0.0, shares)
    sell_quantity = floor(sell_quantity / 100) * 100
    if sell_quantity <= 0:
        return cash, shares, None
    notional = sell_quantity * execution_price
    commission = max(minimum_commission, notional * commission_rate)
    fee = commission + notional * stamp_duty_rate
    return cash + notional - fee, shares - sell_quantity, {
        "side": side, "quantity": sell_quantity, "execution_price": execution_price,
        "notional": notional, "fee": fee, "commission": commission, "amount": notional,
    }


def execute_open_nav_trade(
    side: str,
    nav: float,
    *,
    cash: float,
    shares: float,
    amount: float,
    fee_rate: float,
) -> tuple[float, float, dict[str, Any] | None]:
    """按开放式基金的现金份额和公布净值成交，份额保留八位小数。"""

    if side not in {"buy", "sell"} or nav <= 0:
        raise ValueError("交易方向或基金净值无效")
    if side == "buy":
        gross = min(max(0.0, cash), max(0.0, amount))
        fee = gross * fee_rate
        quantity = floor((gross - fee) / nav * 100_000_000) / 100_000_000
        notional = quantity * nav
        spent = notional + fee
        while quantity > 0 and spent > cash + 0.00001:
            quantity = max(0.0, quantity - 0.00000001)
            notional = quantity * nav
            spent = notional + fee
        if quantity <= 0:
            return cash, shares, None
        return cash - spent, shares + quantity, {
            "side": side, "quantity": quantity, "execution_price": nav,
            "notional": notional, "fee": fee, "amount": spent,
        }
    quantity = floor(min(max(0.0, shares), max(0.0, amount)) * 100_000_000) / 100_000_000
    if quantity <= 0:
        return cash, shares, None
    notional = quantity * nav
    fee = notional * fee_rate
    return cash + notional - fee, shares - quantity, {
        "side": side, "quantity": quantity, "execution_price": nav,
        "notional": notional, "fee": fee, "amount": notional,
    }
