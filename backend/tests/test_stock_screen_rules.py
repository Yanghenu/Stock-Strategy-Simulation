"""选股信号的边界测试：未来行情不能进入排名。"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Bar
from app.stock_screen.rules import score_symbol, rank_candidates


def bars(closes: list[float]) -> list[Bar]:
    return [Bar(date(2025, 1, 1) + timedelta(days=i), c, c, c, c, c, c, c, c, turnover=20_000_000)
            for i, c in enumerate(closes)]


class ScreenRulesTests(unittest.TestCase):
    def test_score_uses_only_completed_bars_before_cutoff(self):
        history = bars([100 + i * 0.1 for i in range(261)])
        signal = score_symbol(history, history[-1].trade_date)
        future = bars([100 + i * 0.1 for i in range(261)] + [10000])
        self.assertEqual(score_symbol(future, history[-1].trade_date), signal)
        self.assertIsNotNone(signal)
        self.assertGreater(signal[0], 0)

    def test_insufficient_history_and_invalid_prices_are_excluded(self):
        self.assertIsNone(score_symbol(bars([10] * 200), date(2026, 1, 1)))
        self.assertIsNone(score_symbol(bars([10] * 240 + [0]), date(2026, 1, 1)))

    def test_each_rule_ranks_independently_and_ties_deterministically(self):
        rows = [
            {"symbol": "000002", "momentum_score": 0.2, "reversal_score": 0.1},
            {"symbol": "000001", "momentum_score": 0.2, "reversal_score": 0.3},
            {"symbol": "000003", "momentum_score": None, "reversal_score": None},
        ]
        ranked = rank_candidates(rows, 1)
        self.assertEqual(ranked["000001"], (1, 1))
        self.assertEqual(ranked["000002"], (None, None))

