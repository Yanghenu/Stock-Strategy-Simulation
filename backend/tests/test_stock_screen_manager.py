"""逐股恢复、真实未来收益和候选汇总的离线验证。"""

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import StockScreenSettings
from app.market_data.service import MarketDataServiceError
from app.models import Bar, Quote
from app.stock_screen.manager import StockScreenManager
from app.stock_screen.repository import _retry_deadlock_read


def sample_bars(count: int, start: date = date(2025, 1, 1)) -> list[Bar]:
    return [Bar(start + timedelta(days=i), 10 + i / 100, 10 + i / 100, 10 + i / 100,
                10 + i / 100, 10 + i / 100, 10 + i / 100, 10 + i / 100,
                10 + i / 100, turnover=20_000_000) for i in range(count)]


class FakeRepository:
    def __init__(self):
        self.pending = [{"symbol": "600000", "name": "浦发银行"}]
        self.saved = []
        self.finalized = False
        self.error = None
        self.evaluations = []

    def set_running(self, run_id):
        pass

    def next_pending_item(self, run_id):
        return self.pending.pop(0) if self.pending else None

    def save_signal(self, run_id, symbol, score):
        self.saved.append((symbol, score))

    def set_source_error(self, run_id, message):
        self.error = message

    def finalize_run(self, run_id, candidate_count):
        self.finalized = True

    def due_evaluations(self, today, limit=20):
        return [{"run_id": "r", "symbol": "600000", "signal_date": date(2025, 1, 1), "signal_close": 10}]

    def save_evaluation(self, run_id, symbol, today, outcomes):
        self.evaluations = outcomes


class FakeMarket:
    def __init__(self, bars):
        self.bars = bars
        self.error = None

    def get_bars(self, symbol, start, end):
        if self.error:
            raise self.error
        return self.bars

    def get_screening_universe(self, limit, minimum_turnover):
        return [Quote("600000", "浦发银行", 10, 0, 0, 10, 10, 10, 10, 100, 20000000,
                      "test", datetime.now(timezone.utc))]


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepository()
        self.market = FakeMarket(sample_bars(261))
        self.now = datetime(2025, 10, 20, 17, tzinfo=timezone(timedelta(hours=8)))
        self.manager = StockScreenManager(self.repo, self.market,
                                          StockScreenSettings(request_gap_seconds=0), clock=lambda: self.now)

    def test_process_one_item_saves_checkpoint_and_finishes(self):
        self.manager._process_run({"run_id": "r", "signal_cutoff": date(2025, 10, 20), "candidate_count": 20})
        self.assertEqual(len(self.repo.saved), 1)
        self.assertIsNotNone(self.repo.saved[0][1])
        self.assertTrue(self.repo.finalized)

    def test_provider_error_keeps_item_for_retry(self):
        self.market.error = RuntimeError("provider offline")
        self.manager._process_run({"run_id": "r", "signal_cutoff": date(2025, 10, 20), "candidate_count": 20})
        self.assertFalse(self.repo.finalized)
        self.assertEqual(self.repo.saved, [])
        self.assertIn("600000", self.repo.error)

    def test_missing_history_excludes_only_that_symbol(self):
        self.market.error = MarketDataServiceError("600000 在所选日期范围内没有历史行情")
        self.manager._process_run({"run_id": "r", "signal_cutoff": date(2025, 10, 20), "candidate_count": 20})
        self.assertEqual(self.repo.saved, [("600000", None)])
        self.assertTrue(self.repo.finalized)

    def test_future_outcomes_do_not_use_bars_after_today(self):
        self.now = datetime(2025, 1, 21, 17, tzinfo=timezone(timedelta(hours=8)))
        self.market.bars = sample_bars(22)
        self.manager._evaluate_due()
        self.assertEqual([row[0] for row in self.repo.evaluations], [5, 20])
        self.assertEqual(self.repo.evaluations[1][1], date(2025, 1, 21))

    def test_scan_rejected_before_close(self):
        self.now = datetime(2025, 10, 20, 10, tzinfo=timezone(timedelta(hours=8)))
        with self.assertRaisesRegex(ValueError, "16:00"):
            self.manager.create()

    def test_read_retries_only_sql_deadlock(self):
        calls = []

        @_retry_deadlock_read
        def transient():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Serialization failure; SQL Server 死锁牺牲品")
            return "ok"

        self.assertEqual(transient(), "ok")
        self.assertEqual(len(calls), 2)

        @_retry_deadlock_read
        def permanent():
            raise RuntimeError("invalid SQL")

        with self.assertRaisesRegex(RuntimeError, "invalid SQL"):
            permanent()

    def test_benchmark_includes_data_covered_stocks_without_signal(self):
        class ResultsRepository(FakeRepository):
            def get_run(self, run_id):
                return {
                    "run_id": run_id, "status": "completed", "requested_count": 2,
                    "items": [
                        {"symbol": "000001", "status": "eligible", "momentum_rank": 1, "reversal_rank": None},
                        {"symbol": "000002", "status": "eligible", "momentum_rank": None, "reversal_rank": None},
                    ],
                    "outcomes": [
                        {"symbol": "000001", "horizon_days": 5, "return_rate": 0.1},
                        {"symbol": "000002", "horizon_days": 5, "return_rate": -0.1},
                    ],
                }

        manager = StockScreenManager(ResultsRepository(), self.market, StockScreenSettings())
        result = manager.get_run("r")
        self.assertEqual(result["eligible_count"], 2)
        momentum_5 = next(row for row in result["metrics"] if row["strategy_code"] == "momentum_trend"
                          and row["horizon_days"] == 5)
        self.assertAlmostEqual(momentum_5["mean_return"], 0.1)
        self.assertAlmostEqual(momentum_5["benchmark_return"], 0)


if __name__ == "__main__":
    unittest.main()
