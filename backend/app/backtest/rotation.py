"""组合强弱轮动的历史逐日撮合；行情在任务开始时一次加载。"""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import floor

from app.backtest.engine import LOT_SIZE, _fee, _validate_inputs, validate_backtest_params
from app.backtest.metrics import calculate_metrics
from app.models import (
    BacktestParams, BacktestResult, Bar, ClosedTrade, CorporateAction, EquityPoint, Trade,
)
from app.paper_trading.strategy import rank_momentum_rotation


@dataclass
class _Holding:
    shares: int
    entry_date: date
    cost_basis: float
    dividends: float = 0.0


def _affordable_quantity(budget: float, price: float, params: BacktestParams) -> int:
    """从目标现金倒推整手数量，最低佣金也计入预算。"""

    quantity = floor(budget / price / LOT_SIZE) * LOT_SIZE
    while quantity > 0:
        notional = quantity * price
        if notional + _fee(notional, params.commission_rate, params.minimum_commission) <= budget:
            return quantity
        quantity -= LOT_SIZE
    return 0


def run_rotation_backtest(
    bars_by_symbol: dict[str, list[Bar]],
    actions_by_symbol: dict[str, list[CorporateAction]],
    params: BacktestParams,
    cancel_check=None,
    progress_callback=None,
) -> BacktestResult:
    """仅使用已收盘 K 线排名，次日开盘先卖后买并遵守整手和 T+1。"""

    validate_backtest_params(params)
    symbols = params.symbols or (params.symbol,)
    if params.strategy_code != "momentum_rotation" or set(symbols) != set(bars_by_symbol):
        raise ValueError("轮动回测股票列表与历史行情不一致")
    for symbol in symbols:
        _validate_inputs(bars_by_symbol[symbol], params)

    by_date = {symbol: {bar.trade_date: bar for bar in bars_by_symbol[symbol]} for symbol in symbols}
    active_dates = sorted({
        bar.trade_date for symbol in symbols for bar in bars_by_symbol[symbol]
        if params.start_date <= bar.trade_date <= params.end_date
    })
    if not active_dates:
        raise ValueError("回测区间没有可用交易日")

    action_map: dict[date, list[tuple[str, CorporateAction]]] = defaultdict(list)
    warnings: list[str] = []
    for symbol in symbols:
        for action in actions_by_symbol.get(symbol, []):
            if action.ex_date is None or action.status != "implemented":
                warnings.append(f"{symbol} 的分红配送事件日期或实施状态未确认，回测未计入。")
            elif params.start_date <= action.ex_date <= params.end_date:
                action_map[action.ex_date].append((symbol, action))

    cash = params.initial_capital
    holdings: dict[str, _Holding] = {}
    last_close: dict[str, float] = {}
    history: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
    history_index = {symbol: 0 for symbol in symbols}
    holdings_by_date: dict[str, dict[date, int]] = {symbol: {} for symbol in symbols}
    trades: list[Trade] = []
    closed_trades: list[ClosedTrade] = []
    closed_symbols: list[str] = []
    equity_curve: list[EquityPoint] = []
    fees_total = 0.0
    dividends_total = 0.0
    peak = params.initial_capital
    pending: tuple[date, list[str]] | None = None
    last_rebalanced: date | None = None

    # 组合基准是同一股票池开盘等权买入并持有；费用、滑点与策略使用相同设置。
    benchmark_cash = params.initial_capital
    benchmark_shares = {symbol: 0 for symbol in symbols}
    benchmark_holdings_by_date: dict[str, dict[date, int]] = {symbol: {} for symbol in symbols}
    benchmark_started = False

    for completed, current_date in enumerate(active_dates, start=1):
        if cancel_check is not None and cancel_check():
            raise InterruptedError("回测已取消")
        today = {symbol: by_date[symbol].get(current_date) for symbol in symbols}

        # 除权日使用登记日快照分配权益；卖出后到账的股息归属原已平仓交易。
        for symbol, action in action_map.get(current_date, []):
            entitled = holdings_by_date[symbol].get(action.record_date, 0) if action.record_date else 0
            if entitled:
                dividend = entitled * action.cash_per_share
                bonus = int(entitled * action.bonus_shares_per_share)
                cash += dividend
                dividends_total += dividend
                holding = holdings.get(symbol)
                if holding and action.record_date and holding.entry_date <= action.record_date:
                    holding.dividends += dividend
                else:
                    for index in range(len(closed_trades) - 1, -1, -1):
                        closed = closed_trades[index]
                        if closed_symbols[index] == symbol and action.record_date and closed.entry_date <= action.record_date < closed.exit_date:
                            profit = closed.profit_loss + dividend
                            closed_trades[index] = replace(
                                closed, profit_loss=profit,
                                return_rate=profit / closed.cost_basis if closed.cost_basis else None,
                            )
                            break
                if bonus:
                    if holding is None:
                        holdings[symbol] = _Holding(bonus, current_date, 0)
                    else:
                        holding.shares += bonus
            benchmark_entitled = (
                benchmark_holdings_by_date[symbol].get(action.record_date, 0)
                if action.record_date else 0
            )
            if benchmark_entitled:
                benchmark_cash += benchmark_entitled * action.cash_per_share
                benchmark_shares[symbol] += int(benchmark_entitled * action.bonus_shares_per_share)

        # 股票停牌或缺当日 K 线时不做部分组合成交，等待全部标的都有有效开盘价。
        if pending is not None and all(today.values()):
            signal_date, selected = pending
            opening_equity = cash + sum(
                holding.shares * today[symbol].open for symbol, holding in holdings.items()
            )
            target_budget = opening_equity * params.position_ratio / params.max_positions
            selected_set = set(selected)
            for symbol in symbols:
                holding = holdings.get(symbol)
                if holding is None or current_date <= holding.entry_date:
                    continue
                bar = today[symbol]
                target_shares = (
                    floor(target_budget / bar.open / LOT_SIZE) * LOT_SIZE if symbol in selected_set else 0
                )
                quantity = floor(max(0, holding.shares - target_shares) / LOT_SIZE) * LOT_SIZE
                if quantity <= 0:
                    continue
                price = bar.open * (1 - params.slippage_rate)
                notional = quantity * price
                commission = _fee(notional, params.commission_rate, params.minimum_commission)
                stamp_duty = notional * params.stamp_duty_rate
                proceeds = notional - commission - stamp_duty
                if cash + proceeds < 0:
                    continue
                ratio = quantity / holding.shares
                cost = holding.cost_basis * ratio
                dividend = holding.dividends * ratio
                profit = proceeds - cost + dividend
                cash += proceeds
                fees_total += commission + stamp_duty
                closed_trades.append(ClosedTrade(
                    holding.entry_date, current_date, quantity, profit,
                    profit / cost if cost else None, cost,
                ))
                closed_symbols.append(symbol)
                trades.append(Trade(
                    current_date, signal_date, "sell", price, quantity, notional,
                    commission, stamp_duty,
                    "高于等权目标仓位" if symbol in selected_set else "跌出强弱轮动目标组合", symbol,
                ))
                holding.shares -= quantity
                holding.cost_basis -= cost
                holding.dividends -= dividend
                if holding.shares == 0:
                    del holdings[symbol]

            for symbol in selected:
                bar = today[symbol]
                existing = holdings.get(symbol)
                current_value = (existing.shares if existing else 0) * bar.open
                deficit = max(0.0, target_budget - current_value)
                price = bar.open * (1 + params.slippage_rate)
                quantity = _affordable_quantity(min(deficit, cash), price, params)
                if quantity <= 0:
                    continue
                notional = quantity * price
                commission = _fee(notional, params.commission_rate, params.minimum_commission)
                cash -= notional + commission
                fees_total += commission
                if existing:
                    existing.shares += quantity
                    existing.cost_basis += notional + commission
                    # 与纸面账户一致：追加买入后整个混合持仓按 T+1 等待次日可卖。
                    existing.entry_date = current_date
                else:
                    holdings[symbol] = _Holding(quantity, current_date, notional + commission)
                trades.append(Trade(
                    current_date, signal_date, "buy", price, quantity, notional,
                    commission, 0, "按动量排名买入目标股票", symbol,
                ))
            last_rebalanced = current_date
            pending = None

        if not benchmark_started and all(today.values()):
            budget = params.initial_capital * params.position_ratio / len(symbols)
            for symbol in symbols:
                price = today[symbol].open * (1 + params.slippage_rate)
                quantity = _affordable_quantity(min(budget, benchmark_cash), price, params)
                if quantity:
                    notional = quantity * price
                    benchmark_cash -= notional + _fee(notional, params.commission_rate, params.minimum_commission)
                    benchmark_shares[symbol] = quantity
            benchmark_started = True

        for symbol in symbols:
            bar = today[symbol]
            if bar:
                last_close[symbol] = bar.close
            holdings_by_date[symbol][current_date] = holdings.get(symbol).shares if symbol in holdings else 0
            benchmark_holdings_by_date[symbol][current_date] = benchmark_shares[symbol]

        market_value = sum(
            holding.shares * last_close.get(symbol, 0) for symbol, holding in holdings.items()
        )
        equity = cash + market_value
        peak = max(peak, equity)
        benchmark_equity = benchmark_cash + sum(
            shares * last_close.get(symbol, 0) for symbol, shares in benchmark_shares.items()
        )
        equity_curve.append(EquityPoint(
            current_date, cash, sum(holding.shares for holding in holdings.values()),
            market_value, equity, equity / peak - 1 if peak else 0, benchmark_equity,
        ))

        # 指针只向前移动，每只股票的预热数据只扫描一次；排名仅读取当前及以前完整日线。
        if all(today.values()):
            for symbol in symbols:
                bars = bars_by_symbol[symbol]
                while history_index[symbol] < len(bars) and bars[history_index[symbol]].trade_date <= current_date:
                    history[symbol].append(bars[history_index[symbol]])
                    history_index[symbol] += 1
            if pending is None and (
                last_rebalanced is None
                or current_date - last_rebalanced >= timedelta(days=params.rebalance_interval_days)
            ):
                selected = rank_momentum_rotation(
                    history, params.momentum_period_days, params.trend_ma_period,
                    params.max_positions,
                )
                if selected is not None:
                    pending = (current_date, selected)
        if progress_callback is not None:
            progress_callback(completed, len(active_dates))

    metrics = calculate_metrics(
        params.initial_capital, [point.equity for point in equity_curve], closed_trades,
    )
    return BacktestResult(
        symbol=params.symbol, metrics=metrics, trades=trades, closed_trades=closed_trades,
        equity_curve=equity_curve, fees_total=fees_total, dividends_total=dividends_total,
        benchmark_return=equity_curve[-1].benchmark_equity / params.initial_capital - 1,
        warnings=warnings,
    )
