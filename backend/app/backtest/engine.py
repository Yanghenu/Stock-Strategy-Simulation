"""双均线日线撮合引擎；不访问网络、FastAPI 或数据库。"""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from math import floor, isfinite

from app.backtest.metrics import calculate_metrics
from app.paper_trading.strategy import (
    evaluate_defensive_mean_reversion,
    find_entry_atr,
    should_exit,
)
from app.models import (
    BacktestParams,
    BacktestResult,
    Bar,
    ClosedTrade,
    CorporateAction,
    EquityPoint,
    OpenPosition,
    Trade,
)

LOT_SIZE = 100


@dataclass
class _Position:
    """引擎内部持仓账本，现金分红与买入成本分开累计。"""

    entry_date: date
    shares: int
    cost_basis: float
    dividends: float = 0


def validate_backtest_params(params: BacktestParams) -> None:
    """在创建持久化任务前校验周期、资金、费率与日期边界。"""

    if params.strategy_code not in (
        "dual_ma", "breakout_ma_atr", "momentum_rotation", "defensive_mean_reversion",
    ):
        raise ValueError("不支持的回测策略")
    symbols = params.symbols or (params.symbol,)
    if (not symbols or len(symbols) > 20 or len(set(symbols)) != len(symbols)
            or symbols[0] != params.symbol):
        raise ValueError("回测股票列表必须以主股票开头、不能重复且最多 20 只")
    if params.strategy_code != "momentum_rotation" and len(symbols) != 1:
        raise ValueError("单股策略只能回测一只股票")
    if params.strategy_code == "dual_ma" and (
        params.short_window < 1 or params.long_window < 1 or params.short_window >= params.long_window
    ):
        raise ValueError("均线周期必须为正数，且短周期小于长周期")
    if params.strategy_code == "breakout_ma_atr" and (
        min(params.ma_period, params.breakout_period, params.atr_period) <= 0
        or not 0 < params.atr_multiplier <= 10
    ):
        raise ValueError("趋势突破策略参数无效")
    if params.strategy_code == "momentum_rotation" and (
        min(params.momentum_period_days, params.trend_ma_period,
            params.max_positions, params.rebalance_interval_days) <= 0
        or params.max_positions > 20
    ):
        raise ValueError("组合轮动策略参数无效")
    if params.strategy_code == "defensive_mean_reversion" and (
        not 21 <= params.trend_ma_period <= 250
        or not 1 <= params.atr_period <= 100
        or not isfinite(params.atr_multiplier) or not 0.1 <= params.atr_multiplier <= 10
        or not 1 <= params.max_holding_days <= 120
        or not 0 <= params.cooldown_days <= 120
        or not isfinite(params.target_annual_volatility)
        or not 0.01 <= params.target_annual_volatility <= 2
        or not isfinite(params.position_ratio) or not 0.01 <= params.position_ratio <= 1
    ):
        raise ValueError("防御型均值回归策略参数无效")
    if not isfinite(params.initial_capital) or params.initial_capital <= 0:
        raise ValueError("初始资金必须大于 0")
    if not 0 < params.position_ratio <= 1:
        raise ValueError("仓位比例必须大于 0 且不超过 1")
    if any(value < 0 for value in (params.commission_rate, params.minimum_commission, params.stamp_duty_rate)):
        raise ValueError("费率和最低佣金不能为负数")
    if not 0 <= params.slippage_rate < 1:
        raise ValueError("滑点比例必须大于等于 0 且小于 1")
    if params.start_date > params.end_date:
        raise ValueError("回测开始日期不能晚于结束日期")


def _validate_inputs(bars: list[Bar], params: BacktestParams) -> None:
    """在计算前拒绝不完整或不符合账户边界的数据。"""

    validate_backtest_params(params)
    if not bars:
        raise ValueError("没有可用的历史行情")
    dates = [bar.trade_date for bar in bars]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ValueError("行情日期必须按升序排列且不能重复")
    for bar in bars:
        prices = (bar.open, bar.high, bar.low, bar.close, bar.adj_close)
        if any(not isfinite(price) or price <= 0 for price in prices):
            raise ValueError(f"{bar.trade_date} 行情存在非正或无效价格")
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close):
            raise ValueError(f"{bar.trade_date} OHLC 价格关系不正确")


def _fee(notional: float, rate: float, minimum: float = 0) -> float:
    """计算佣金；最低佣金只应用于实际发生的买卖委托。"""

    return max(minimum, notional * rate)


def _whole_lot_quantity(cash: float, budget_ratio: float, price: float, params: BacktestParams) -> int:
    """将仓位限制、整手和最低佣金一起纳入可买数量。"""

    budget = min(cash, cash * budget_ratio)
    quantity = floor(budget / price / LOT_SIZE) * LOT_SIZE
    while quantity > 0:
        notional = quantity * price
        if notional + _fee(notional, params.commission_rate, params.minimum_commission) <= budget:
            return quantity
        quantity -= LOT_SIZE
    return 0


def _buy_holdings(
    cash: float,
    shares: int,
    entry_date: date | None,
    bar: Bar,
    params: BacktestParams,
    budget_ratio: float | None = None,
) -> tuple[float, int, date | None]:
    """基准在首个回测日开盘按给定资金比例买入整手股票。"""

    price = bar.open * (1 + params.slippage_rate)
    quantity = _whole_lot_quantity(
        cash, params.position_ratio if budget_ratio is None else budget_ratio, price, params,
    )
    if quantity == 0:
        return cash, shares, entry_date
    notional = quantity * price
    cash -= notional + _fee(notional, params.commission_rate, params.minimum_commission)
    return cash, quantity, bar.trade_date


def run_backtest(
    bars: list[Bar],
    actions: list[CorporateAction],
    params: BacktestParams,
    cancel_check=None,
    progress_callback=None,
) -> BacktestResult:
    """运行单股回测；预热行情只用于策略信号，不计入回测账户。"""

    _validate_inputs(bars, params)
    action_map: dict[date, list[CorporateAction]] = defaultdict(list)
    action_warnings: list[str] = []
    for action in actions:
        if action.ex_date is None or action.status != "implemented":
            action_warnings.append(
                f"{action.symbol} 的分红配送事件日期或实施状态未确认，回测未计入。"
            )
        else:
            action_map[action.ex_date].append(action)

    # 前缀和一次性预计算，使任意周期的移动平均可在 O(1) 时间取值。
    prefix = [0.0]
    for bar in bars:
        prefix.append(prefix[-1] + bar.adj_close)

    def average(end_index: int, period: int) -> float:
        return (prefix[end_index + 1] - prefix[end_index + 1 - period]) / period

    active_indices = [
        i for i, bar in enumerate(bars)
        if params.start_date <= bar.trade_date <= params.end_date
    ]
    if not active_indices:
        raise ValueError("回测区间没有可用交易日")

    cash = params.initial_capital
    shares = 0
    position: _Position | None = None
    trades: list[Trade] = []
    closed_trades: list[ClosedTrade] = []
    equity_curve: list[EquityPoint] = []
    holdings_by_date: dict[date, int] = {}
    dividends_total = 0.0
    fees_total = 0.0
    pending: tuple[str, date, str] | None = None
    pending_atr: float | None = None
    # 买入信号的波动率仓位随待成交单保留到下一交易日开盘撮合。
    pending_position_ratio: float | None = None
    entry_atr: float | None = None
    entry_price: float | None = None
    highest_price: float | None = None
    last_exit_date: date | None = None
    bonus_liquidation_date: date | None = None
    peak = params.initial_capital
    benchmark_cash = params.initial_capital
    benchmark_shares = 0
    benchmark_holding_date: date | None = None
    benchmark_holdings_by_date: dict[date, int] = {}

    for completed, index in enumerate(active_indices, start=1):
        bar = bars[index]
        bonus_shares_added_today = 0
        if cancel_check is not None and cancel_check():
            # 取消使用异常中断，调用层不会把部分曲线和成交写入数据库。
            raise InterruptedError("回测已取消")

        # 除权日按登记日收盘持仓计入现金分红和送转股，基准执行相同规则。
        for action in action_map.get(bar.trade_date, []):
            entitled = holdings_by_date.get(action.record_date, 0) if action.record_date else 0
            if entitled:
                dividend = entitled * action.cash_per_share
                bonus = int(entitled * action.bonus_shares_per_share)
                # 登記日持有人即使在除權日前賣出，現金分紅仍歸其所有。
                cash += dividend
                dividends_total += dividend
                if position and position.entry_date <= action.record_date:
                    position.dividends += dividend
                else:
                    # 已平倉的那筆交易仍需計入登記日後才到帳的分紅。
                    for trade_index in range(len(closed_trades) - 1, -1, -1):
                        closed = closed_trades[trade_index]
                        if closed.entry_date <= action.record_date < closed.exit_date:
                            profit = closed.profit_loss + dividend
                            closed_trades[trade_index] = replace(
                                closed,
                                profit_loss=profit,
                                return_rate=profit / closed.cost_basis if closed.cost_basis else None,
                            )
                            break
                if bonus:
                    bonus_shares_added_today += bonus
                    # 送轉股是已取得的持倉權益；原倉位若已賣出，建立零現金成本的持倉批次。
                    if position is None:
                        position = _Position(bar.trade_date, bonus, 0)
                        if params.strategy_code == "defensive_mean_reversion":
                            # 记录权益到账日，供防御策略遵守 T+1 限制。
                            bonus_liquidation_date = bar.trade_date
                    else:
                        position.shares += bonus
                    shares += bonus

            benchmark_entitled = (
                benchmark_holdings_by_date.get(action.record_date, 0) if action.record_date else 0
            )
            if benchmark_entitled and benchmark_shares:
                benchmark_cash += benchmark_entitled * action.cash_per_share
                benchmark_shares += int(benchmark_entitled * action.bonus_shares_per_share)

        # 待执行委托来自前一交易日收盘信号，买卖成交都使用本日原始开盘价。
        if pending is not None:
            side, signal_date, reason = pending
            if side == "buy" and position is None:
                price = bar.open * (1 + params.slippage_rate)
                buy_ratio = (
                    pending_position_ratio if params.strategy_code == "defensive_mean_reversion"
                    else params.position_ratio
                )
                quantity = _whole_lot_quantity(cash, buy_ratio or 0, price, params)
                if quantity:
                    notional = quantity * price
                    commission = _fee(notional, params.commission_rate, params.minimum_commission)
                    cash -= notional + commission
                    shares = quantity
                    position = _Position(bar.trade_date, quantity, notional + commission)
                    entry_atr = pending_atr
                    entry_price = price
                    highest_price = bar.high
                    fees_total += commission
                    trades.append(Trade(bar.trade_date, signal_date, side, price, quantity, notional, commission, 0, reason))
            elif side == "sell" and position is not None and bar.trade_date > position.entry_date:
                price = bar.open * (1 - params.slippage_rate)
                # 防御策略遵守 T+1；除权日新到账的股份不能随旧仓当天卖出。
                sell_quantity = (
                    shares - bonus_shares_added_today
                    if params.strategy_code == "defensive_mean_reversion"
                    else shares
                )
                if sell_quantity > 0:
                    notional = sell_quantity * price
                    commission = _fee(notional, params.commission_rate, params.minimum_commission)
                    stamp_duty = notional * params.stamp_duty_rate
                    cash += notional - commission - stamp_duty
                    fees_total += commission + stamp_duty
                    realized = notional - commission - stamp_duty - position.cost_basis + position.dividends
                    closed_trades.append(
                        ClosedTrade(
                            position.entry_date,
                            bar.trade_date,
                            sell_quantity,
                            realized,
                            realized / position.cost_basis if position.cost_basis else None,
                            position.cost_basis,
                        )
                    )
                    trades.append(Trade(bar.trade_date, signal_date, side, price, sell_quantity, notional, commission, stamp_duty, reason))
                    shares -= sell_quantity
                    if shares:
                        # 新到账送转股与原仓成本分开，留到次日再撮合退出。
                        position = _Position(bar.trade_date, shares, 0)
                    else:
                        position = None
                        entry_atr = None
                        entry_price = None
                        highest_price = None
                    if params.strategy_code == "defensive_mean_reversion":
                        # 冷却从真实成交日起算；保留的新股次日再退出。
                        last_exit_date = bar.trade_date
                        bonus_liquidation_date = bar.trade_date if shares else None
            pending = None
            pending_atr = None
            pending_position_ratio = None

        # 基准在首个回测日开盘买入并持有，费用与滑点参数和策略保持一致。
        if benchmark_holding_date is None:
            benchmark_cash, benchmark_shares, benchmark_holding_date = _buy_holdings(
                benchmark_cash, benchmark_shares, benchmark_holding_date,
                bar, params,
                1.0 if params.strategy_code == "defensive_mean_reversion" else None,
            )

        market_value = shares * bar.close
        equity = cash + market_value
        peak = max(peak, equity)
        benchmark_equity = benchmark_cash + benchmark_shares * bar.close
        equity_curve.append(
            EquityPoint(
                bar.trade_date, cash, shares, market_value, equity,
                equity / peak - 1 if peak else 0, benchmark_equity,
            )
        )
        holdings_by_date[bar.trade_date] = shares
        benchmark_holdings_by_date[bar.trade_date] = benchmark_shares

        # 所有策略都在收盘后用已完成日线决策，防御策略不读取盘中执行报价。
        if params.strategy_code == "defensive_mean_reversion":
            if position is not None and bonus_liquidation_date == bar.trade_date:
                # 送转股权益不等待 RSI/ATR 入场状态，在可交易的下一日退出。
                pending = ("sell", bar.trade_date, "退出除权新增股份")
            else:
                decision = evaluate_defensive_mean_reversion(
                    bars[:index + 1],
                    position.entry_date if position else None,
                    entry_price,
                    entry_atr,
                    last_exit_date,
                    params.trend_ma_period,
                    params.atr_period,
                    params.atr_multiplier,
                    params.max_holding_days,
                    params.cooldown_days,
                    params.target_annual_volatility,
                    params.position_ratio,
                )
                if decision.action == "buy" and position is None:
                    pending = ("buy", bar.trade_date, decision.reason)
                    pending_atr = decision.entry_atr
                    pending_position_ratio = decision.position_ratio
                elif decision.action == "sell" and position is not None:
                    pending = ("sell", bar.trade_date, decision.reason)
        elif params.strategy_code == "breakout_ma_atr":
            if position is None:
                required = max(params.ma_period, params.breakout_period, params.atr_period + 1)
                atr = find_entry_atr(
                    bars[max(0, index - required):index], bar.close,
                    params.ma_period, params.breakout_period, params.atr_period,
                )
                if atr is not None:
                    pending = ("buy", bar.trade_date, "站上趋势均线并突破前期高点")
                    pending_atr = atr
            else:
                highest_price = max(highest_price or bar.high, bar.high)
                if should_exit(bar.close, highest_price, entry_atr or 0, params.atr_multiplier):
                    pending = ("sell", bar.trade_date, "跌破 ATR 跟踪止损")
        elif index >= params.long_window - 1:
            short_now = average(index, params.short_window)
            long_now = average(index, params.long_window)
            short_before = average(index - 1, params.short_window) if index >= params.long_window else None
            long_before = average(index - 1, params.long_window) if index >= params.long_window else None
            if short_before is not None and long_before is not None:
                if short_before <= long_before and short_now > long_now:
                    pending = ("buy", bar.trade_date, "短均线上穿长均线")
                elif short_before >= long_before and short_now < long_now:
                    pending = ("sell", bar.trade_date, "短均线下穿长均线")

        if progress_callback is not None:
            progress_callback(completed, len(active_indices))

    equity_values = [point.equity for point in equity_curve]
    metrics = calculate_metrics(params.initial_capital, equity_values, closed_trades)
    final_benchmark = equity_curve[-1].benchmark_equity
    return BacktestResult(
        symbol=params.symbol,
        metrics=metrics,
        trades=trades,
        closed_trades=closed_trades,
        equity_curve=equity_curve,
        fees_total=fees_total,
        dividends_total=dividends_total,
        benchmark_return=final_benchmark / params.initial_capital - 1,
        open_position=(
            OpenPosition(position.entry_date, position.shares, position.cost_basis, position.dividends)
            if position
            else None
        ),
        warnings=action_warnings,
    )
