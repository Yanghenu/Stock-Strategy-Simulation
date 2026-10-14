"""固定版研究信号；输入仅包含截至信号日的已完成日线。"""

from datetime import date
from math import isfinite
from typing import Any

from app.models import Bar


def score_symbol(bars: list[Bar], cutoff: date) -> tuple[float | None, float | None, date, float] | None:
    """计算 120/240 日中期动量与 5 日反转，跳过近 20 日动量以减轻短期反转干扰。

    返回原始 5 日收益（第二项）；整个批次的均值在排名阶段才可得，减去同一常数不改变名次。
    """

    completed = sorted((bar for bar in bars if bar.trade_date <= cutoff), key=lambda bar: bar.trade_date)
    if len(completed) < 261:
        return None
    closes = [bar.adj_close for bar in completed]
    if any(not isfinite(value) or value <= 0 for value in closes[-261:]):
        return None
    current = closes[-1]
    ma60 = sum(closes[-60:]) / 60
    ma120 = sum(closes[-120:]) / 120
    momentum = None
    reversal = None
    if current > ma120:
        # -21 是二十个交易日前，-141/-261 分别再向前 120/240 个交易日。
        momentum = 0.5 * (closes[-21] / closes[-141] - 1) + 0.5 * (closes[-21] / closes[-261] - 1)
    if current > ma60:
        reversal = closes[-1] / closes[-6] - 1
    return momentum, reversal, completed[-1].trade_date, current


def rank_candidates(rows: list[dict[str, Any]], count: int) -> dict[str, tuple[int | None, int | None]]:
    """两种规则独立排名；同分按代码保证重启后结果稳定。"""

    ranks: dict[str, list[int | None]] = {str(row["symbol"]): [None, None] for row in rows}
    for position, key in enumerate(("momentum_score", "reversal_score")):
        eligible = [row for row in rows if row.get(key) is not None]
        eligible.sort(key=lambda row: (-float(row[key]), str(row["symbol"])))
        for rank, row in enumerate(eligible[:count], start=1):
            ranks[str(row["symbol"])][position] = rank
    return {symbol: (values[0], values[1]) for symbol, values in ranks.items()}
