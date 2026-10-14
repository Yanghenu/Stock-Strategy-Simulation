"""防御型均值回归策略的纯计算边界测试。"""

import unittest
from datetime import date, timedelta
from inspect import signature
from math import nan, inf

from app.models import Bar
from app.paper_trading.strategy import evaluate_defensive_mean_reversion


def make_bars(closes: list[float]) -> list[Bar]:
    """用连续真实交易日和原始 OHLC 构造策略输入。"""
    start = date(2025, 1, 2)
    bars = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        trade_day = start
        remaining = index
        while remaining:
            trade_day += timedelta(days=1)
            if trade_day.weekday() < 5:
                remaining -= 1
        bars.append(Bar(trade_day, previous, max(previous, close) + 0.5,
                        min(previous, close) - 0.5, close, previous, max(previous, close) + 0.5,
                        min(previous, close) - 0.5, close))
    return bars


def evaluate(bars, **overrides):
    params = dict(entry_date=None, entry_price=None, entry_atr=None, last_exit_date=None,
                  trend_ma_period=30, atr_period=14, atr_multiplier=2.0, max_holding_days=5,
                  cooldown_days=3, target_annual_volatility=0.15, max_position_ratio=0.70)
    params.update(overrides)
    return evaluate_defensive_mean_reversion(bars, **params)


class DefensiveStrategyTests(unittest.TestCase):
    def test_catalog_contains_new_strategy_and_independent_parameter_copies(self):
        from app.paper_trading.strategy import get_strategy_catalog
        catalog = get_strategy_catalog()
        item = next(row for row in catalog if row["code"] == "defensive_mean_reversion")
        item["parameters"][0]["default"] = -1
        again = next(row for row in get_strategy_catalog() if row["code"] == "defensive_mean_reversion")
        self.assertNotEqual(again["parameters"][0]["default"], -1)

    def test_long_term_trend_filter_blocks_entries(self):
        bars = make_bars([100 - i * 0.2 for i in range(60)])
        self.assertIsNone(evaluate(bars).action)

    def test_oversold_entry_and_high_volatility_reduces_position(self):
        closes = [70.0 + i * 0.8 for i in range(40)] + [101.2] * 20
        closes[-2:] = [101.2, 100.0]
        decision = evaluate(make_bars(closes), target_annual_volatility=0.01)
        self.assertEqual(decision.action, "buy")
        self.assertIsNotNone(decision.entry_atr)
        self.assertLess(decision.position_ratio, 0.70)

    def test_higher_realized_volatility_reduces_position(self):
        base = [70.0 + i * 0.8 for i in range(40)] + [101.2] * 20
        base[-2:] = [101.2, 100.0]
        volatile = list(base)
        volatile[39] = 70.0
        low = evaluate(make_bars(base))
        high = evaluate(make_bars(volatile))
        self.assertEqual((low.action, high.action), ("buy", "buy"))
        self.assertLess(high.position_ratio, low.position_ratio)

    def test_raw_ohlc_drives_signals_and_no_intraday_quote_argument(self):
        closes = [70.0 + i * 0.8 for i in range(40)] + [101.2] * 20
        closes[-2:] = [101.2, 100.0]
        bars = make_bars(closes)
        baseline = evaluate(bars)
        self.assertEqual(baseline.action, "buy")
        adjusted = [Bar(b.trade_date, b.open, b.high, b.low, b.close,
                        b.open * 100, b.high * 100, b.low * 100, b.close * 100) for b in bars]
        self.assertEqual(evaluate(adjusted), baseline)
        self.assertNotIn("quote", " ".join(signature(evaluate_defensive_mean_reversion).parameters).lower())

    def test_insufficient_daily_history_and_invalid_atr_do_not_trade(self):
        bars = make_bars([100.0] * 20)
        self.assertIsNone(evaluate(bars).action)
        enough = make_bars([100.0] * 60)
        self.assertIsNone(evaluate(enough, atr_period=0).action)

    def test_zero_atr_and_nonfinite_position_state_are_rejected(self):
        flat = []
        for bar in make_bars([100.0] * 60):
            flat.append(Bar(bar.trade_date, 100, 100, 100, 100, 100, 100, 100, 100))
        self.assertEqual(evaluate(flat).reason, "invalid_atr")
        bars = make_bars([100.0 + i * 0.1 for i in range(65)])
        for bad in (nan, inf):
            self.assertEqual(evaluate(bars, entry_date=bars[-2].trade_date, entry_price=bad,
                                      entry_atr=1).reason, "invalid_position")
            self.assertEqual(evaluate(bars, entry_date=bars[-2].trade_date, entry_price=1,
                                      entry_atr=bad).reason, "invalid_position")

    def test_duplicate_trade_dates_are_rejected(self):
        bars = make_bars([100.0 + i * 0.1 for i in range(65)])
        duplicate = bars + [bars[-1]]
        self.assertEqual(evaluate(duplicate).reason, "duplicate_trade_dates")

    def test_exit_priority_and_conditions(self):
        bars = make_bars([100.0 + i * 0.1 for i in range(65)])
        last = bars[-1]
        # 固定入场数据仅由已收盘信号日决定，不存在盘中报价参数。
        self.assertEqual(evaluate(bars, entry_date=bars[-2].trade_date, entry_price=1,
                                  entry_atr=1, max_holding_days=1).reason, "maximum_holding_period")
        self.assertEqual(evaluate(bars, entry_date=bars[-3].trade_date, entry_price=last.close + 100,
                                  entry_atr=1).reason, "atr_stop")
        self.assertEqual(evaluate(bars, entry_date=bars[-3].trade_date, entry_price=1,
                                  entry_atr=1).reason, "mean_reversion")
        falling = make_bars([160.0 - i * 0.5 for i in range(65)])
        self.assertEqual(evaluate(falling, entry_date=falling[-3].trade_date, entry_price=1,
                                  entry_atr=1).reason, "trend_filter_failed")

    def test_cooldown_excludes_three_full_trading_days_and_allows_fourth(self):
        closes = [70.0 + i * 0.8 for i in range(40)] + [101.2] * 20
        closes[-2:] = [101.2, 100.0]
        bars = make_bars(closes)
        for offset in (1, 2, 3):
            self.assertEqual(evaluate(bars, last_exit_date=bars[-offset-1].trade_date).reason, "cooldown")
        # 冷却期的三个完整交易日之后，D4 才能执行原本成立的入场信号。
        self.assertEqual(evaluate(bars, last_exit_date=bars[-5].trade_date).action, "buy")


if __name__ == "__main__":
    unittest.main()
