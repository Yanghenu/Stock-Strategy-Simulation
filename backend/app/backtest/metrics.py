"""回测绩效统计，显式处理无交易、零波动与短区间。"""

from math import sqrt
from statistics import pstdev

from app.models import ClosedTrade, Metrics


def calculate_metrics(
    initial_capital: float,
    equity_values: list[float],
    closed_trades: list[ClosedTrade],
) -> Metrics:
    """按 252 个交易日年化；夏普假定无风险利率为 0。"""

    if initial_capital <= 0:
        raise ValueError("初始资金必须大于 0")

    final_value = equity_values[-1] if equity_values else initial_capital
    total_return = final_value / initial_capital - 1
    period_count = len(equity_values)
    annualized_return = (
        (final_value / initial_capital) ** (252 / period_count) - 1
        if period_count and final_value > 0
        else None
    )

    # 把初始资金作为首个峰值，确保第一天就亏损时也能正确计算回撤。
    peak = initial_capital
    max_drawdown = 0.0
    for equity in equity_values:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, equity / peak - 1)

    values = [initial_capital, *equity_values]
    daily_returns = [current / previous - 1 for previous, current in zip(values, values[1:]) if previous > 0]
    volatility = pstdev(daily_returns) * sqrt(252) if len(daily_returns) > 1 else None
    daily_std = pstdev(daily_returns) if len(daily_returns) > 1 else 0
    sharpe = (
        sum(daily_returns) / len(daily_returns) / daily_std * sqrt(252)
        if daily_returns and daily_std > 0
        else None
    )
    win_rate = (
        sum(1 for trade in closed_trades if trade.profit_loss > 0) / len(closed_trades)
        if closed_trades
        else None
    )

    return Metrics(
        total_return=total_return,
        annualized_return=annualized_return,
        max_drawdown=max_drawdown,
        annualized_volatility=volatility,
        sharpe_ratio=sharpe,
        win_rate=win_rate,
        closed_trade_count=len(closed_trades),
    )
