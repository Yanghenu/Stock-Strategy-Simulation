import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.metrics import calculate_metrics
from app.models import ClosedTrade


class MetricsTests(unittest.TestCase):
    def test_calculates_return_drawdown_and_win_rate(self) -> None:
        metrics = calculate_metrics(
            initial_capital=100,
            equity_values=[110, 90, 120],
            closed_trades=[
                ClosedTrade(date(2024, 1, 1), date(2024, 1, 2), 100, 10, 0.1),
                ClosedTrade(date(2024, 1, 3), date(2024, 1, 4), 100, -5, -0.05),
            ],
        )

        self.assertAlmostEqual(metrics.total_return, 0.2)
        self.assertAlmostEqual(metrics.max_drawdown, -20 / 110)
        self.assertAlmostEqual(metrics.win_rate, 0.5)

    def test_empty_trades_have_no_win_rate_and_flat_curve_has_no_sharpe(self) -> None:
        metrics = calculate_metrics(initial_capital=100, equity_values=[100, 100], closed_trades=[])

        self.assertIsNone(metrics.win_rate)
        self.assertIsNone(metrics.sharpe_ratio)
        self.assertEqual(metrics.total_return, 0)


if __name__ == "__main__":
    unittest.main()
