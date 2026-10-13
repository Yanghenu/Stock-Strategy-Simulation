import sys
import threading
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import BacktestParams, Bar
from app.services.backtest_manager import BacktestManager


def params() -> BacktestParams:
    return BacktestParams("600000", date(2024, 1, 1), date(2024, 1, 6), 2, 3, 100000, 1, 0, 0, 0, 0)


def bars() -> list[Bar]:
    closes = [10, 9, 8, 12, 10, 8]
    return [
        Bar(date(2024, 1, day), close, close, close, close, close, close, close, close, 0, 0)
        for day, close in enumerate(closes, start=1)
    ]


class FakeRepository:
    def __init__(self) -> None:
        self.status: dict[str, str] = {}
        self.progress: dict[str, int] = {}
        self.saved: set[str] = set()
        self.completed = threading.Event()
        self.cancelled = threading.Event()

    def create_backtest(self, run_id: str, params: BacktestParams) -> None:
        self.status[run_id] = "queued"
        self.progress[run_id] = 0

    def set_backtest_status(self, run_id: str, status: str, progress: int, error_message: str | None = None) -> None:
        self.status[run_id] = status
        self.progress[run_id] = progress
        if status in {"completed", "failed", "cancelled"}:
            self.completed.set()
            if status == "cancelled":
                self.cancelled.set()

    def save_backtest_result(self, run_id: str, result: object) -> None:
        self.saved.add(run_id)
        self.status[run_id] = "completed"
        self.completed.set()

    def request_cancel(self, run_id: str) -> bool:
        if self.status.get(run_id) not in {"queued", "running"}:
            return False
        return True


class FakeMarketData:
    def __init__(self, block: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.ensure_calls = 0
        self.block = block

    def ensure_stock(self, symbol: str) -> None:
        self.ensure_calls += 1

    def get_backtest_inputs(self, symbol: str, start: date, end: date, long_window: int):
        self.started.set()
        if self.block:
            self.release.wait(2)
        return bars(), [], []


class BacktestManagerTests(unittest.TestCase):
    def test_rejects_invalid_params_before_creating_a_database_run(self) -> None:
        repository = FakeRepository()
        manager = BacktestManager(repository, FakeMarketData())

        with self.assertRaises(ValueError):
            manager.submit(replace(params(), short_window=3, long_window=3))
        manager.shutdown()

        self.assertEqual(repository.status, {})

    def test_submits_persists_and_completes_background_run(self) -> None:
        repository = FakeRepository()
        market_data = FakeMarketData()
        manager = BacktestManager(repository, market_data)

        run_id = manager.submit(params())
        self.assertTrue(repository.completed.wait(3))
        manager.shutdown()

        self.assertEqual(repository.status[run_id], "completed")
        self.assertIn(run_id, repository.saved)
        self.assertEqual(market_data.ensure_calls, 1)

    def test_rotation_loads_each_symbol_once_without_touching_paper_manager(self) -> None:
        repository = FakeRepository()
        market_data = FakeMarketData()
        manager = BacktestManager(repository, market_data)

        run_id = manager.submit(replace(
            params(), strategy_code="momentum_rotation", symbols=("600000", "000333"),
        ))
        self.assertTrue(repository.completed.wait(3))
        manager.shutdown()

        self.assertEqual(repository.status[run_id], "completed")
        self.assertEqual(market_data.ensure_calls, 2)

    def test_cancelled_run_does_not_persist_partial_result(self) -> None:
        repository = FakeRepository()
        market_data = FakeMarketData(block=True)
        manager = BacktestManager(repository, market_data)

        run_id = manager.submit(params())
        self.assertTrue(market_data.started.wait(2))
        self.assertTrue(manager.cancel(run_id))
        market_data.release.set()
        self.assertTrue(repository.cancelled.wait(3))
        manager.shutdown()

        self.assertNotIn(run_id, repository.saved)

    def test_cannot_cancel_unknown_or_completed_run(self) -> None:
        manager = BacktestManager(FakeRepository(), FakeMarketData())

        self.assertFalse(manager.cancel("missing"))
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
