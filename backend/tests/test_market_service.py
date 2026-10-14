import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BacktestDefaults, DatabaseSettings, MarketDataSettings, RedisSettings, Settings
from app.market_data.akshare_source import MarketDataSourceError
from app.market_data.service import MarketDataService
from app.models import Bar, CorporateAction, IntradayBar, Quote


def sample_quote(at: datetime) -> Quote:
    return Quote("600000", "浦发银行", 8.2, 0.1, 1.23, 8.1, 8.3, 8.0, 8.1, 100, 820000, "AKShare", at)


def sample_bar() -> Bar:
    return Bar(date(2024, 1, 2), 10, 11, 9, 10, 9, 10, 8, 9, 100, 1000)


class FakeSource:
    def __init__(self) -> None:
        self.quote_calls = 0
        self.bar_calls = 0
        self.action_calls = 0
        self.intraday_calls: list[tuple[str, date]] = []
        self.intraday_bars: list[IntradayBar] = []
        self.recent_intraday_calls: list[tuple[str, date]] = []
        self.recent_intraday_bars: list[IntradayBar] = []
        self.quote_error = False
        self.single_quote_calls = 0

    def fetch_quotes(self) -> list[Quote]:
        self.quote_calls += 1
        if self.quote_error:
            raise MarketDataSourceError("source unavailable")
        return [sample_quote(datetime.now(timezone.utc))]

    def fetch_quote(self, symbol: str) -> Quote:
        self.single_quote_calls += 1
        if self.quote_error:
            raise MarketDataSourceError("source unavailable")
        return sample_quote(datetime.now(timezone.utc))

    def fetch_bars(self, symbol: str, start_date: date, end_date: date) -> list[Bar]:
        self.bar_calls += 1
        return [sample_bar()]

    def fetch_intraday_bars(self, symbol: str, trade_date: date) -> list[IntradayBar]:
        self.intraday_calls.append((symbol, trade_date))
        return self.intraday_bars

    def fetch_recent_intraday_bars(self, symbol: str, end_date: date, lookback_days: int = 14) -> list[IntradayBar]:
        self.recent_intraday_calls.append((symbol, end_date))
        return self.recent_intraday_bars

    def fetch_actions(self, symbol: str) -> tuple[list[CorporateAction], list[str]]:
        self.action_calls += 1
        return ([CorporateAction(symbol, date(2024, 1, 1), date(2024, 1, 2), 0.1)], [])


class FakeCache:
    def __init__(self) -> None:
        self.quotes: dict[str, Quote] = {}
        self.locked = False
        self.quote_locked = False
        self.published = 0
        self.single_published = 0

    def get_quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    def get_quotes(self) -> list[Quote]:
        return list(self.quotes.values())

    def acquire_refresh_lock(self, token: str) -> bool:
        if self.locked:
            return False
        self.locked = True
        return True

    def release_refresh_lock(self, token: str) -> None:
        self.locked = False

    def acquire_quote_refresh_lock(self, symbol: str, token: str) -> bool:
        if self.quote_locked:
            return False
        self.quote_locked = True
        return True

    def release_quote_refresh_lock(self, symbol: str, token: str) -> None:
        self.quote_locked = False

    def publish_snapshot(self, quotes: list[Quote], fetched_at: datetime) -> str:
        self.published += 1
        self.quotes = {item.symbol: item for item in quotes}
        return "generation"

    def publish_quote(self, quote: Quote) -> None:
        self.single_published += 1
        self.quotes[quote.symbol] = quote


class FakeRepository:
    def __init__(self) -> None:
        self.covered = False
        self.bars: list[Bar] = []
        self.actions: list[CorporateAction] = []
        self.saved_stock: tuple[str, str, str | None] | None = None

    def has_bar_coverage(self, symbol: str, start_date: date, end_date: date) -> bool:
        return self.covered

    def get_bars(self, symbol: str, start_date: date, end_date: date) -> list[Bar]:
        return self.bars

    def upsert_stock(self, symbol: str, name: str, exchange: str | None) -> None:
        self.saved_stock = (symbol, name, exchange)

    def upsert_bars(self, symbol: str, bars: list[Bar]) -> None:
        self.bars = bars

    def mark_bar_coverage(self, symbol: str, start_date: date, end_date: date) -> None:
        self.covered = True

    def upsert_actions(self, symbol: str, actions: list[CorporateAction]) -> None:
        self.actions = actions

    def get_actions(self, symbol: str, start_date: date, end_date: date) -> list[CorporateAction]:
        return self.actions


def settings() -> Settings:
    return Settings(
        DatabaseSettings("localhost", "QuantBacktest", "windows", "", "", True, True, 8, 30),
        RedisSettings("127.0.0.1", 6379, 0, "", 3, 300, 60, 180),
        MarketDataSettings(20, 2),
        BacktestDefaults(100000, 5, 20, 0.0003, 5, 0.001, 0.0005),
    )


class MarketDataServiceTests(unittest.TestCase):
    def test_intraday_service_only_returns_bars_for_current_china_trade_date(self) -> None:
        today = date(2026, 10, 13)
        source = FakeSource()
        source.intraday_bars = [
            IntradayBar(datetime(2026, 10, 12, 15, 0, tzinfo=timezone(timedelta(hours=8))), 9, 9, 9, 9),
            IntradayBar(datetime(2026, 10, 13, 9, 31, tzinfo=timezone(timedelta(hours=8))), 9.6, 9.7, 9.5, 9.65),
        ]
        service = MarketDataService(
            FakeRepository(), source, FakeCache(), settings(),
            clock=lambda: datetime(2026, 10, 13, 1, 35, tzinfo=timezone.utc),
        )

        bars = service.get_intraday_bars("SH600000")

        self.assertEqual(source.intraday_calls, [("600000", today)])
        self.assertEqual([bar.trade_time.date() for bar in bars], [today])
        self.assertEqual(bars[0].close, 9.65)

    def test_intraday_returns_latest_available_session_on_weekend(self) -> None:
        source = FakeSource()
        source.recent_intraday_bars = [
            IntradayBar(datetime(2026, 10, 12, 15, 0, tzinfo=timezone(timedelta(hours=8))), 9, 9, 9, 9),
            IntradayBar(datetime(2026, 10, 16, 15, 0, tzinfo=timezone(timedelta(hours=8))), 10, 10, 10, 10),
            IntradayBar(datetime(2026, 10, 19, 9, 31, tzinfo=timezone(timedelta(hours=8))), 11, 11, 11, 11),
        ]
        service = MarketDataService(
            FakeRepository(), source, FakeCache(), settings(),
            clock=lambda: datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc),
        )

        bars = service.get_intraday_bars("600000")

        self.assertEqual([bar.trade_time.date() for bar in bars], [date(2026, 10, 16)])
        self.assertEqual(source.intraday_calls, [("600000", date(2026, 10, 18))])
        self.assertEqual(source.recent_intraday_calls, [("600000", date(2026, 10, 18))])

    def test_intraday_reuses_recent_session_fallback_during_polling(self) -> None:
        source = FakeSource()
        source.recent_intraday_bars = [
            IntradayBar(datetime(2026, 10, 16, 15, 0, tzinfo=timezone(timedelta(hours=8))), 10, 10, 10, 10),
        ]
        service = MarketDataService(
            FakeRepository(), source, FakeCache(), settings(),
            clock=lambda: datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc),
        )

        first = service.get_intraday_bars("600000")
        second = service.get_intraday_bars("600000")

        self.assertEqual(first, second)
        self.assertEqual(len(source.recent_intraday_calls), 1)

    def test_reuses_fresh_quote_and_returns_old_snapshot_as_stale_on_source_error(self) -> None:
        now = datetime(2024, 1, 2, tzinfo=timezone.utc)
        cache = FakeCache()
        source = FakeSource()
        service = MarketDataService(FakeRepository(), source, cache, settings(), clock=lambda: now)

        first = service.get_quote("600000")
        now += timedelta(seconds=20)
        second = service.get_quote("600000")
        source.quote_error = True
        now += timedelta(seconds=61)
        stale = service.get_quote("600000")

        self.assertEqual(source.quote_calls, 2)
        self.assertEqual(source.single_quote_calls, 0)
        self.assertEqual(cache.published, 1)
        self.assertEqual(cache.single_published, 0)
        self.assertFalse(first.stale)
        self.assertFalse(second.stale)
        self.assertTrue(stale.stale)

    def test_manual_refresh_bypasses_fresh_snapshot_interval(self) -> None:
        now = datetime(2024, 1, 2, tzinfo=timezone.utc)
        cache = FakeCache()
        cache.quotes["600000"] = sample_quote(now)
        source = FakeSource()
        service = MarketDataService(FakeRepository(), source, cache, settings(), clock=lambda: now)

        result = service.get_quote("600000", force_refresh=True)

        self.assertEqual(source.quote_calls, 1)
        self.assertEqual(source.single_quote_calls, 0)
        self.assertEqual(cache.published, 1)
        self.assertEqual(cache.single_published, 0)
        self.assertFalse(result.stale)

    def test_search_filters_code_and_name_from_shared_snapshot(self) -> None:
        now = datetime(2024, 1, 2, tzinfo=timezone.utc)
        cache = FakeCache()
        cache.quotes["600000"] = sample_quote(now)
        service = MarketDataService(FakeRepository(), FakeSource(), cache, settings(), clock=lambda: now)

        results = service.search_stocks("浦发")

        self.assertEqual([item.symbol for item in results], ["600000"])

    def test_uses_fresh_snapshot_published_by_another_worker(self) -> None:
        now = datetime(2024, 1, 2, tzinfo=timezone.utc)

        class RacingCache(FakeCache):
            def acquire_refresh_lock(self, token: str) -> bool:
                # 模拟另一个 worker 在本请求等待 Redis 锁时刚发布了新版本。
                self.quotes["600000"] = sample_quote(now)
                return False

        cache = RacingCache()
        cache.quotes["600000"] = sample_quote(now - timedelta(seconds=61))
        service = MarketDataService(FakeRepository(), FakeSource(), cache, settings(), clock=lambda: now)

        results = service.search_stocks("浦发")

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].stale)

    def test_history_fetch_is_persisted_with_stock_and_coverage(self) -> None:
        repository = FakeRepository()
        source = FakeSource()
        now = datetime(2024, 1, 2, tzinfo=timezone.utc)
        cache = FakeCache()
        cache.quotes["600000"] = sample_quote(now)
        service = MarketDataService(repository, source, cache, settings(), clock=lambda: now)

        bars = service.get_bars("600000", date(2024, 1, 1), date(2024, 1, 31))

        self.assertEqual(len(bars), 1)
        self.assertEqual(source.bar_calls, 1)
        self.assertTrue(repository.covered)
        self.assertEqual(repository.saved_stock, ("600000", "浦发银行", "SH"))

    def test_backtest_inputs_include_warmup_and_persist_actions(self) -> None:
        repository = FakeRepository()
        source = FakeSource()
        service = MarketDataService(repository, source, FakeCache(), settings())

        bars, actions, warnings = service.get_backtest_inputs(
            "600000", date(2024, 1, 1), date(2024, 1, 31), long_window=20
        )

        self.assertEqual(len(bars), 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(warnings, [])
        self.assertEqual(source.action_calls, 1)
        self.assertLess(source.bar_calls, 3)


if __name__ == "__main__":
    unittest.main()
