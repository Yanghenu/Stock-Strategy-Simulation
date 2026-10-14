import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Bar
from app.models import Quote
from app.config import BacktestDefaults, DatabaseSettings, MarketDataSettings, PaperTradingSettings, RedisSettings, Settings
from app.paper_trading.manager import CHINA_TZ, PaperTradingManager, is_new_live_quote
from app.paper_trading.strategy import (
    DefensiveDecision, find_dual_ma_signal, find_entry_atr, get_strategy_catalog, rank_momentum_rotation,
    should_exit, trailing_stop,
)


def make_bars(closes: list[float], high: float, low: float) -> list[Bar]:
    start = date(2026, 7, 1)
    return [
        Bar(
            start + timedelta(days=index), close, max(high, close), min(low, close), close,
            close, max(high, close), min(low, close), close,
        )
        for index, close in enumerate(closes)
    ]


def make_weekday_bars(through: date, count: int = 151) -> list[Bar]:
    """构造按交易日排列的日线，避免策略测试把周末误当成交易日。"""

    days = []
    cursor = through
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return [Bar(day, 10, 11, 9, 10, 10, 11, 9, 10) for day in reversed(days)]


ROTATION_SYMBOLS = ["000333", "600000", "600028", "600276", "600887", "601899"]


def rotation_bar_sets(last_closes: list[float]) -> dict[str, list[Bar]]:
    """为组合策略测试构造足够长且仅在最后收盘价不同的历史序列。"""

    return {
        symbol: make_bars([10.0] * 59 + [last_close], high=max(10.0, last_close), low=min(10.0, last_close))
        for symbol, last_close in zip(ROTATION_SYMBOLS, last_closes)
    }


def rotation_quotes(at: datetime, prices: list[float]) -> dict[str, Quote]:
    return {
        symbol: Quote(
            symbol, symbol, price, 0, 0, price, price, price, price,
            100, 100000, "测试行情", at, False,
        )
        for symbol, price in zip(ROTATION_SYMBOLS, prices)
    }


class MemoryPaperRepository:
    def __init__(self, last_quote_at: datetime | None = None) -> None:
        self.session = {
            "session_id": "paper-1", "task_name": "一个月纸面实验", "strategy_code": "breakout_ma_atr",
            "status": "running", "ends_at": datetime(2026, 11, 12, 9, 30),
            "initial_capital": 100000, "cash": 100000, "commission_rate": 0.0003,
            "minimum_commission": 5, "stamp_duty_rate": 0.001, "slippage_rate": 0.0005,
            "ma_period": 60, "breakout_period": 20, "atr_period": 20, "atr_multiplier": 2,
            "short_ma_period": 5, "long_ma_period": 20,
            "momentum_period_days": 20, "trend_ma_period": 60,
            "max_holding_days": 5, "cooldown_days": 3,
            "target_annual_volatility": 0.15, "max_position_ratio": 0.7,
            "max_positions": 3, "rebalance_interval_days": 7, "last_rebalanced_at": None,
        }
        self.symbol = {
            "symbol": "600000", "budget": 100000, "shares": 0, "entry_date": None,
            "entry_price": None, "cost_basis": 0, "highest_price": None, "atr_value": None,
            "last_price": None, "last_quote_at": last_quote_at,
            "last_exit_date": None, "last_decision_date": None,
        }
        self.trades: list[dict[str, object]] = []
        self.sessions = [self.session]
        self.symbols_by_session = {"paper-1": [self.symbol]}
        self.trades_by_session = {"paper-1": self.trades}
        self.errors: dict[str, str | None] = {}
        self.completed_sessions: list[str] = []
        self.created: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.stopped_sessions: list[str] = []
        self.equity_updates = 0
        self.rebalance_calls: list[dict[str, object]] = []
        self.quote_calls: list[dict[str, object]] = []
        self.reject_next_quote = False
        self.completed = False

    def add_session(
        self, session_id: str, capital: float, strategy_code: str, symbol: str,
        budget: float, ends_at: datetime | None = None,
    ) -> None:
        session = {
            **self.session, "session_id": session_id, "task_name": session_id,
            "strategy_code": strategy_code, "ends_at": ends_at,
            "initial_capital": capital, "cash": capital,
        }
        self.sessions.append(session)
        self.symbols_by_session[session_id] = [{
            **self.symbol, "symbol": symbol, "budget": budget, "last_quote_at": None,
        }]
        self.trades_by_session[session_id] = []

    def get_active_paper_session(self):
        return self.session

    def get_active_paper_sessions(self):
        return [session for session in self.sessions if session["status"] == "running"]

    def get_paper_symbols(self, session_id: str):
        return [symbol.copy() for symbol in self.symbols_by_session[session_id]]

    def apply_paper_quote(self, session_id, symbol, quote_at, state, cash, trade, processed_at=None, decision_date=None):
        self.quote_calls.append({"quote_at": quote_at, "cash": cash, "trade": trade, "decision_date": decision_date})
        session = next(session for session in self.sessions if session["session_id"] == session_id)
        ends_at = session.get("ends_at")
        if ends_at is not None and processed_at is not None:
            # SQL DATETIME2 以无时区墙上时间保存；测试仓储按上海时区对齐后比较。
            process_time = processed_at.astimezone(CHINA_TZ).replace(tzinfo=None) if processed_at.tzinfo else processed_at
            expiry_time = ends_at.astimezone(CHINA_TZ).replace(tzinfo=None) if ends_at.tzinfo else ends_at
            if process_time >= expiry_time:
                self.complete_paper_session(session_id, processed_at)
                return False
        row = next(row for row in self.symbols_by_session[session_id] if row["symbol"] == symbol)
        if row["last_quote_at"] and quote_at <= row["last_quote_at"]:
            return False
        if decision_date and row.get("last_decision_date") and decision_date <= row["last_decision_date"]:
            return False
        if self.reject_next_quote:
            self.reject_next_quote = False
            return False
        row.update(state)
        row["last_quote_at"] = quote_at
        if decision_date is not None:
            row["last_decision_date"] = decision_date
        session = next(session for session in self.sessions if session["session_id"] == session_id)
        session["cash"] = cash
        if trade:
            self.trades_by_session[session_id].append(trade)
        return True

    def apply_paper_rebalance(
        self, session_id, processed_at, cash, symbol_updates, trades, rebalanced_at,
    ):
        session = self.get_paper_session(session_id)
        if not session or session["status"] != "running":
            return False
        rows = {row["symbol"]: row for row in self.symbols_by_session[session_id]}
        if set(rows) != {item["symbol"] for item in symbol_updates}:
            return False
        for item in symbol_updates:
            previous = rows[item["symbol"]]["last_quote_at"]
            if previous and item["last_quote_at"] <= previous:
                return False
        for item in symbol_updates:
            rows[item["symbol"]].update(item)
        session["cash"] = cash
        if rebalanced_at is not None:
            session["last_rebalanced_at"] = rebalanced_at
        self.trades_by_session[session_id].extend(trades)
        if session_id == "paper-1":
            self.trades.extend(trades)
        self.rebalance_calls.append({
            "session_id": session_id, "processed_at": processed_at, "cash": cash,
            "symbol_updates": symbol_updates, "trades": trades, "rebalanced_at": rebalanced_at,
        })
        self.equity_updates += 1
        return True

    def update_paper_equity(self, _session_id: str, _trade_date: date):
        self.equity_updates += 1

    def complete_paper_session(self, session_id: str, _completed_at: datetime):
        self.completed = True
        self.completed_sessions.append(session_id)
        next(session for session in self.sessions if session["session_id"] == session_id)["status"] = "completed"

    def set_paper_session_error(self, session_id: str, message: str | None):
        self.errors[session_id] = message

    def create_paper_session(self, *args, **kwargs):
        self.created.append((args, kwargs))
        session_id, started_at, ends_at, capital = args[:4]
        symbol_budgets = args[12]
        session = {
            "session_id": session_id, "task_name": kwargs["task_name"],
            "strategy_code": kwargs["strategy_code"], "status": "running", "started_at": started_at,
            "ends_at": ends_at, "initial_capital": capital, "cash": capital,
            "commission_rate": args[4], "minimum_commission": args[5],
            "stamp_duty_rate": args[6], "slippage_rate": args[7], "ma_period": args[8],
            "breakout_period": args[9], "atr_period": args[10], "atr_multiplier": args[11],
            "short_ma_period": kwargs["short_ma_period"], "long_ma_period": kwargs["long_ma_period"],
            "momentum_period_days": kwargs.get("momentum_period_days", 20),
            "trend_ma_period": kwargs.get("trend_ma_period", 60),
            "max_holding_days": kwargs.get("max_holding_days", 5),
            "cooldown_days": kwargs.get("cooldown_days", 3),
            "target_annual_volatility": kwargs.get("target_annual_volatility", 0.15),
            "max_position_ratio": kwargs.get("max_position_ratio", 0.7),
            "max_holding_days": kwargs.get("max_holding_days", 5),
            "cooldown_days": kwargs.get("cooldown_days", 3),
            "target_annual_volatility": kwargs.get("target_annual_volatility", 0.15),
            "max_position_ratio": kwargs.get("max_position_ratio", 0.7),
            "max_positions": kwargs.get("max_positions", 3),
            "rebalance_interval_days": kwargs.get("rebalance_interval_days", 7),
            "last_rebalanced_at": None,
        }
        self.sessions.append(session)
        self.symbols_by_session[session_id] = [
            {"symbol": symbol, "budget": budget, "shares": 0, "entry_date": None,
             "entry_price": None, "cost_basis": 0, "highest_price": None, "atr_value": None,
             "last_price": None, "last_quote_at": None,
             "last_exit_date": None, "last_decision_date": None}
            for symbol, budget in symbol_budgets
        ]
        self.trades_by_session[session_id] = []

    def get_paper_session(self, session_id: str):
        return next((session for session in self.sessions if session["session_id"] == session_id), None)

    def list_paper_sessions(self):
        return self.sessions

    def stop_paper_session(self, session_id: str, _stopped_at: datetime):
        session = self.get_paper_session(session_id)
        if not session or session["status"] != "running":
            return False
        session["status"] = "stopped"
        self.stopped_sessions.append(session_id)
        return True


class MemoryPaperMarketData:
    def __init__(
        self, quote: Quote, bars: list[Bar], fail_symbols: set[str] | None = None,
        quotes_by_symbol: dict[str, Quote] | None = None,
        bars_by_symbol: dict[str, list[Bar]] | None = None,
    ) -> None:
        self.quote = quote
        self.bars = bars
        self.quotes_by_symbol = quotes_by_symbol or {}
        self.bars_by_symbol = bars_by_symbol or {}
        self.fail_symbols = fail_symbols or set()
        self.force_refresh_values: list[bool] = []
        self.quote_calls: list[str] = []
        self.ensured_symbols: list[str] = []
        self.get_bars_calls: list[tuple[str, date, date]] = []
        self.after_bars = None

    def get_quote(self, symbol: str, force_refresh: bool = False):
        self.force_refresh_values.append(force_refresh)
        self.quote_calls.append(symbol)
        if symbol in self.fail_symbols:
            raise RuntimeError("quote unavailable")
        return self.quotes_by_symbol.get(symbol, self.quote)

    def get_bars(self, symbol: str, _start: date, _end: date):
        self.get_bars_calls.append((symbol, _start, _end))
        bars = self.bars_by_symbol.get(symbol, self.bars)
        if self.after_bars:
            self.after_bars()
        return bars

    def ensure_stock(self, symbol: str):
        self.ensured_symbols.append(symbol)


def paper_settings() -> Settings:
    return Settings(
        DatabaseSettings("localhost", "QuantBacktest", "windows", "", "", True, True, 8, 30),
        RedisSettings("127.0.0.1", 6379, 0, "", 3, 300, 60, 180),
        MarketDataSettings(20, 2),
        BacktestDefaults(100000, 5, 20, 0.0003, 5, 0.001, 0.0005),
        paper_trading=PaperTradingSettings(symbols=("600000",)),
    )


class PaperStrategyTests(unittest.TestCase):
    def test_dual_ma_buys_only_on_upward_cross_and_sells_while_bearish(self) -> None:
        rising_cross = make_bars([1.0] * 5, high=1.0, low=1.0)
        already_rising = make_bars([1, 2, 3, 4, 5], high=5, low=1)
        bearish = make_bars([5, 4, 3, 2, 1], high=5, low=1)

        self.assertEqual(find_dual_ma_signal(rising_cross, 3, 3, 5), "buy")
        self.assertIsNone(find_dual_ma_signal(already_rising, 6, 3, 5))
        self.assertEqual(find_dual_ma_signal(bearish, 1, 3, 5), "sell")

    def test_dual_ma_rejects_insufficient_bars_and_invalid_periods(self) -> None:
        self.assertIsNone(find_dual_ma_signal(make_bars([1, 1, 1, 1], high=1, low=1), 2, 3, 5))
        with self.assertRaises(ValueError):
            find_dual_ma_signal(make_bars([1] * 5, high=1, low=1), 2, 5, 5)

    def test_dual_ma_handles_one_day_short_window(self) -> None:
        bars = make_bars([1, 3], high=3, low=1)

        self.assertEqual(find_dual_ma_signal(bars, 2, 1, 2), "sell")

    def test_strategy_catalog_contains_supported_default_strategies(self) -> None:
        catalog = get_strategy_catalog()

        self.assertEqual(
            {item["code"] for item in catalog},
            {"breakout_ma_atr", "defensive_mean_reversion", "dual_ma", "momentum_rotation"},
        )
        dual_ma = next(item for item in catalog if item["code"] == "dual_ma")
        self.assertEqual((dual_ma["short_ma_period"], dual_ma["long_ma_period"]), (5, 20))
        self.assertEqual(
            [item["key"] for item in dual_ma["parameters"]],
            ["short_ma_period", "long_ma_period"],
        )
        self.assertTrue(all(item["type"] == "integer" for item in dual_ma["parameters"]))
        rotation = next(item for item in catalog if item["code"] == "momentum_rotation")
        self.assertEqual(
            [item["key"] for item in rotation["parameters"]],
            ["momentum_period_days", "trend_ma_period", "max_positions", "rebalance_interval_days"],
        )
        self.assertEqual(
            [item["default"] for item in rotation["parameters"]], [20, 60, 3, 7],
        )

    def test_momentum_rotation_ranks_positive_momentum_above_trend(self) -> None:
        bars = {
            "600003": make_bars([10, 10, 10, 10, 11], high=11, low=10),
            "600002": make_bars([10, 10, 10, 10, 12], high=12, low=10),
            "600001": make_bars([10, 10, 10, 11, 10], high=11, low=10),
            "600004": make_bars([10, 10, 10, 10, 10], high=10, low=10),
        }

        selected = rank_momentum_rotation(bars, momentum_period_days=2, trend_ma_period=3, max_positions=3)

        self.assertEqual(selected, ["600002", "600003"])

    def test_momentum_rotation_uses_symbol_as_deterministic_tie_breaker(self) -> None:
        bars = {
            "600002": make_bars([10, 10, 11], high=11, low=10),
            "600001": make_bars([10, 10, 11], high=11, low=10),
        }

        selected = rank_momentum_rotation(bars, momentum_period_days=1, trend_ma_period=2, max_positions=2)

        self.assertEqual(selected, ["600001", "600002"])

    def test_momentum_rotation_skips_incomplete_constituent_history(self) -> None:
        bars = {
            "600001": make_bars([10, 11, 12], high=12, low=10),
            "600002": make_bars([10, 11], high=11, low=10),
        }

        self.assertIsNone(rank_momentum_rotation(bars, momentum_period_days=2, trend_ma_period=3, max_positions=2))

    def test_momentum_rotation_returns_empty_when_all_stocks_fail_filters(self) -> None:
        bars = {
            "600001": make_bars([10, 10, 10, 10], high=10, low=10),
            "600002": make_bars([10, 10, 11, 10], high=11, low=10),
        }

        self.assertEqual(rank_momentum_rotation(bars, momentum_period_days=2, trend_ma_period=3, max_positions=2), [])

    def test_rotation_experiment_creation_saves_parameters_and_dynamic_budgets(self) -> None:
        started = datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)
        quotes = rotation_quotes(started + timedelta(minutes=1), [10] * len(ROTATION_SYMBOLS))
        repository = MemoryPaperRepository()
        manager = PaperTradingManager(
            repository, MemoryPaperMarketData(quotes[ROTATION_SYMBOLS[0]], [], quotes_by_symbol=quotes),
            paper_settings(), clock=lambda: started,
        )

        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
            {"momentum_period_days": 15, "trend_ma_period": 45, "max_positions": 2, "rebalance_interval_days": 5},
        )

        saved = repository.get_paper_session(session_id)
        self.assertEqual(
            [saved[key] for key in ("momentum_period_days", "trend_ma_period", "max_positions", "rebalance_interval_days")],
            [15, 45, 2, 5],
        )
        self.assertEqual([row["budget"] for row in repository.get_paper_symbols(session_id)], [0] * 6)

    def test_rotation_buys_top_three_then_marks_quotes_without_resetting_weekly_timer(self) -> None:
        now = [datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)]
        market_data = MemoryPaperMarketData(
            rotation_quotes(now[0] + timedelta(minutes=1), [13, 12, 11, 10.8, 10.5, 10.2])[ROTATION_SYMBOLS[0]],
            [],
            quotes_by_symbol=rotation_quotes(now[0] + timedelta(minutes=1), [13, 12, 11, 10.8, 10.5, 10.2]),
            bars_by_symbol=rotation_bar_sets([13, 12, 11, 10.8, 10.5, 10.2]),
        )
        repository = MemoryPaperRepository()
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: now[0])
        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
        )

        manager.run_cycle()

        rows = {item["symbol"]: item for item in repository.get_paper_symbols(session_id)}
        self.assertEqual([symbol for symbol in ROTATION_SYMBOLS if rows[symbol]["shares"] > 0], ROTATION_SYMBOLS[:3])
        self.assertEqual(len(repository.trades_by_session[session_id]), 3)
        self.assertEqual(repository.get_paper_session(session_id)["last_rebalanced_at"], now[0])
        self.assertAlmostEqual(rows[ROTATION_SYMBOLS[0]]["budget"], 100000 / 3)

        now[0] += timedelta(days=1)
        market_data.quotes_by_symbol = rotation_quotes(now[0] + timedelta(minutes=1), [13.1, 12.1, 11.1, 10.9, 10.6, 10.3])
        manager.run_cycle()

        self.assertEqual(repository.get_paper_session(session_id)["last_rebalanced_at"], now[0] - timedelta(days=1))
        self.assertEqual(repository.rebalance_calls[-1]["rebalanced_at"], None)

    def test_rotation_does_not_rebalance_when_any_symbol_history_is_incomplete(self) -> None:
        started = datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)
        quotes = rotation_quotes(started + timedelta(minutes=1), [13, 12, 11, 10.8, 10.5, 10.2])
        bars = rotation_bar_sets([13, 12, 11, 10.8, 10.5, 10.2])
        bars[ROTATION_SYMBOLS[-1]] = bars[ROTATION_SYMBOLS[-1]][:40]
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(
            quotes[ROTATION_SYMBOLS[0]], [], quotes_by_symbol=quotes, bars_by_symbol=bars,
        )
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)
        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
        )

        manager.run_cycle()

        self.assertEqual(repository.rebalance_calls, [])
        self.assertIsNone(repository.get_paper_session(session_id)["last_rebalanced_at"])

    def test_rotation_waits_until_seven_days_to_sell_and_replace_winners(self) -> None:
        now = [datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)]
        bars = rotation_bar_sets([13, 12, 11, 10.8, 10.5, 10.2])
        quotes = rotation_quotes(now[0] + timedelta(minutes=1), [13, 12, 11, 10.8, 10.5, 10.2])
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(quotes[ROTATION_SYMBOLS[0]], [], quotes_by_symbol=quotes, bars_by_symbol=bars)
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: now[0])
        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
        )
        manager.run_cycle()
        first_trades = len(repository.trades_by_session[session_id])

        now[0] += timedelta(days=7)
        bars.update(rotation_bar_sets([10, 10, 10, 13, 12, 11]))
        market_data.quotes_by_symbol = rotation_quotes(now[0] + timedelta(minutes=1), [10, 10, 10, 13, 12, 11])
        manager.run_cycle()

        trades = repository.trades_by_session[session_id][first_trades:]
        self.assertEqual(sum(trade["side"] == "sell" for trade in trades), 3)
        self.assertEqual(sum(trade["side"] == "buy" for trade in trades), 3)
        self.assertEqual(repository.get_paper_session(session_id)["last_rebalanced_at"], now[0])

    def test_rotation_does_not_sell_a_same_day_holding_when_t_plus_one_blocks_exit(self) -> None:
        now = datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)
        quotes = rotation_quotes(now + timedelta(minutes=1), [10] * len(ROTATION_SYMBOLS))
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(
            quotes[ROTATION_SYMBOLS[0]], [], quotes_by_symbol=quotes,
            bars_by_symbol=rotation_bar_sets([10] * len(ROTATION_SYMBOLS)),
        )
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: now)
        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
        )
        # get_paper_symbols 模拟仓储的隔离快照；准备既有仓位时直接改底层持仓行。
        held = repository.symbols_by_session[session_id][0]
        held.update({"shares": 100, "entry_date": now.date(), "entry_price": 10, "cost_basis": 1005})
        repository.get_paper_session(session_id)["cash"] = 98995

        manager.run_cycle()

        self.assertEqual(repository.get_paper_symbols(session_id)[0]["shares"], 100)
        self.assertEqual(repository.trades_by_session[session_id], [])

    def test_rotation_keeps_unused_slots_in_cash_when_fewer_than_three_stocks_qualify(self) -> None:
        now = datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)
        closes = [13, 10, 10, 10, 10, 10]
        quotes = rotation_quotes(now + timedelta(minutes=1), closes)
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(
            quotes[ROTATION_SYMBOLS[0]], [], quotes_by_symbol=quotes,
            bars_by_symbol=rotation_bar_sets(closes),
        )
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: now)
        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
        )

        manager.run_cycle()

        rows = repository.get_paper_symbols(session_id)
        self.assertGreater(rows[0]["shares"], 0)
        self.assertEqual([row["shares"] for row in rows[1:]], [0] * 5)
        self.assertGreater(repository.get_paper_session(session_id)["cash"], 65000)

    def test_rotation_does_not_backfill_quotes_from_before_process_start(self) -> None:
        started = datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)
        quotes = rotation_quotes(started - timedelta(minutes=1), [13, 12, 11, 10.8, 10.5, 10.2])
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(
            quotes[ROTATION_SYMBOLS[0]], [], quotes_by_symbol=quotes,
            bars_by_symbol=rotation_bar_sets([13, 12, 11, 10.8, 10.5, 10.2]),
        )
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)
        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
        )

        manager.run_cycle()

        self.assertEqual(repository.rebalance_calls, [])
        self.assertIsNone(repository.get_paper_session(session_id)["last_rebalanced_at"])

    def test_rotation_honors_stop_signal_while_loading_history(self) -> None:
        now = datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)
        quotes = rotation_quotes(now + timedelta(minutes=1), [13, 12, 11, 10.8, 10.5, 10.2])
        repository = MemoryPaperRepository()
        repository.session["status"] = "stopped"
        market_data = MemoryPaperMarketData(
            quotes[ROTATION_SYMBOLS[0]], [], quotes_by_symbol=quotes,
            bars_by_symbol=rotation_bar_sets([13, 12, 11, 10.8, 10.5, 10.2]),
        )
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: now)
        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
        )
        market_data.after_bars = manager._stop.set

        manager.run_cycle()

        self.assertEqual(repository.rebalance_calls, [])
        self.assertIsNone(repository.get_paper_session(session_id)["last_rebalanced_at"])

    def test_rotation_never_creates_negative_cash_to_pay_minimum_sale_fees(self) -> None:
        now = datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)
        quotes = rotation_quotes(now + timedelta(minutes=1), [0.01] * len(ROTATION_SYMBOLS))
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(
            quotes[ROTATION_SYMBOLS[0]], [], quotes_by_symbol=quotes,
            bars_by_symbol=rotation_bar_sets([10] * len(ROTATION_SYMBOLS)),
        )
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: now)
        session_id = manager.create_experiment(
            "强弱轮动组合实验", ROTATION_SYMBOLS, "momentum_rotation", 100000,
        )
        holding = repository.symbols_by_session[session_id][0]
        holding.update({"shares": 100, "entry_date": now.date() - timedelta(days=1), "entry_price": 0.01, "cost_basis": 1})
        repository.get_paper_session(session_id)["cash"] = 0.1

        manager.run_cycle()

        self.assertEqual(repository.get_paper_symbols(session_id)[0]["shares"], 100)
        self.assertEqual(repository.get_paper_session(session_id)["cash"], 0.1)
        self.assertEqual(repository.trades_by_session[session_id], [])

    def test_enters_only_when_price_breaks_twenty_day_high_above_sixty_day_average(self) -> None:
        bars = make_bars([10.0] * 60, high=10.5, low=9.5)

        atr = find_entry_atr(bars, quote_price=10.6)

        self.assertEqual(atr, 1.0)
        self.assertIsNone(find_entry_atr(bars, quote_price=10.4))

    def test_rejects_breakout_when_price_remains_below_trend_average(self) -> None:
        bars = make_bars([12.0] * 40 + [9.0] * 20, high=9.5, low=8.5)

        self.assertIsNone(find_entry_atr(bars, quote_price=9.6))

    def test_trailing_stop_moves_with_highest_price_and_triggers_on_break(self) -> None:
        self.assertEqual(trailing_stop(highest_price=12.0, atr=1.0), 10.0)
        self.assertTrue(should_exit(price=9.99, highest_price=12.0, atr=1.0))
        self.assertFalse(should_exit(price=10.01, highest_price=12.0, atr=1.0))

    def test_restart_ignores_cached_quotes_from_before_process_started(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=timezone(timedelta(hours=8)))
        fresh = Quote(
            "600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 1, 1, "AKShare",
            started + timedelta(seconds=1), False,
        )
        cached = Quote(**{**fresh.__dict__, "fetched_at": started - timedelta(seconds=1)})
        stale = Quote(**{**fresh.__dict__, "stale": True})

        self.assertTrue(is_new_live_quote(fresh, started, None))
        self.assertFalse(is_new_live_quote(cached, started, None))
        self.assertFalse(is_new_live_quote(stale, started, None))
        self.assertFalse(is_new_live_quote(fresh, started, fresh.fetched_at))

    def test_running_paper_session_buys_breakout_using_only_a_new_quote(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        quote = Quote(
            "600000", "浦发银行", 10.6, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", started + timedelta(minutes=1), False,
        )
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(quote, make_bars([10.0] * 60, high=10.5, low=9.5))
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)

        manager.run_cycle()

        self.assertEqual(len(repository.trades), 1)
        self.assertEqual(repository.trades[0]["side"], "buy")
        self.assertEqual(repository.symbol["shares"] % 100, 0)
        self.assertEqual(market_data.force_refresh_values, [True])
        self.assertEqual(repository.equity_updates, 1)

    def test_running_paper_session_does_not_backfill_an_offline_quote(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        quote = Quote(
            "600000", "浦发银行", 10.6, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", started - timedelta(minutes=1), False,
        )
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(quote, make_bars([10.0] * 60, high=10.5, low=9.5))
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)

        manager.run_cycle()

        self.assertEqual(repository.trades, [])
        self.assertEqual(repository.equity_updates, 0)

    def test_multiple_tasks_keep_accounts_isolated_and_share_each_quote(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        quote = Quote(
            "600000", "浦发银行", 10.6, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", started + timedelta(minutes=1), False,
        )
        repository = MemoryPaperRepository()
        repository.session["ends_at"] = None
        repository.add_session("paper-2", 50000, "dual_ma", "600000", 50000)
        market_data = MemoryPaperMarketData(quote, make_bars([10.0] * 60, high=10.5, low=9.5))
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)

        manager.run_cycle()

        self.assertEqual(market_data.quote_calls, ["600000"])
        self.assertEqual(len(repository.trades_by_session["paper-1"]), 1)
        self.assertEqual(len(repository.trades_by_session["paper-2"]), 1)
        self.assertLess(repository.session["cash"], 100000)
        second = repository.get_paper_session("paper-2")
        self.assertLess(second["cash"], 50000)
        self.assertNotEqual(
            repository.trades_by_session["paper-1"][0]["quantity"],
            repository.trades_by_session["paper-2"][0]["quantity"],
        )

    def test_quote_failure_in_one_task_does_not_block_other_tasks(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        quote = Quote(
            "600000", "浦发银行", 10.6, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", started + timedelta(minutes=1), False,
        )
        repository = MemoryPaperRepository()
        repository.session["ends_at"] = None
        repository.add_session("paper-2", 50000, "breakout_ma_atr", "600028", 50000)
        market_data = MemoryPaperMarketData(quote, make_bars([10.0] * 60, high=10.5, low=9.5), {"600000"})
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)

        manager.run_cycle()

        self.assertEqual(repository.errors["paper-1"], "600000 行情处理失败；详见后端日志。")
        self.assertEqual(len(repository.trades_by_session["paper-2"]), 1)

    def test_expired_monthly_task_completes_without_expiring_indefinite_task(self) -> None:
        started = datetime(2026, 10, 12, 15, 1, tzinfo=CHINA_TZ)
        quote = Quote(
            "600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", started + timedelta(minutes=1), False,
        )
        repository = MemoryPaperRepository()
        repository.session["ends_at"] = started - timedelta(seconds=1)
        repository.add_session("paper-2", 50000, "dual_ma", "600028", 50000, ends_at=None)
        manager = PaperTradingManager(repository, MemoryPaperMarketData(quote, []), paper_settings(), clock=lambda: started)

        manager.run_cycle()

        self.assertEqual(repository.completed_sessions, ["paper-1"])
        self.assertEqual(repository.get_paper_session("paper-2")["status"], "running")

    def test_monthly_task_does_not_apply_quote_returned_after_expiry(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        ends_at = started + timedelta(seconds=30)
        quote = Quote(
            "600000", "浦发银行", 10.6, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", ends_at + timedelta(seconds=1), False,
        )
        repository = MemoryPaperRepository()
        repository.session["ends_at"] = ends_at
        clock = [started]

        class ClockAdvancingMarketData(MemoryPaperMarketData):
            def get_quote(self, symbol: str, force_refresh: bool = False):
                result = super().get_quote(symbol, force_refresh)
                clock[0] = ends_at + timedelta(seconds=1)
                return result

        market_data = ClockAdvancingMarketData(quote, make_bars([10.0] * 60, high=10.5, low=9.5))
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: clock[0])

        manager.run_cycle()

        self.assertEqual(repository.trades, [])
        self.assertEqual(repository.symbol["shares"], 0)
        self.assertEqual(repository.completed_sessions, ["paper-1"])

    def test_create_experiment_persists_strategy_and_equal_symbol_budgets(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        quote = Quote(
            "600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", started, False,
        )
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(quote, [])
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)

        session_id = manager.create_experiment("银行策略对比", ["600000", "600028"], "dual_ma", 100000)

        self.assertEqual(session_id, repository.created[0][0][0])
        self.assertEqual(repository.created[0][1]["task_name"], "银行策略对比")
        self.assertEqual(repository.created[0][1]["strategy_code"], "dual_ma")
        self.assertEqual(repository.created[0][0][2], None)
        self.assertEqual(repository.created[0][0][12], [("600000", 50000), ("600028", 50000)])
        self.assertEqual(repository.created[0][1]["short_ma_period"], 5)
        self.assertEqual(repository.created[0][1]["long_ma_period"], 20)

    def test_create_experiment_persists_custom_strategy_parameters(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(Quote(
            "600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", started, False,
        ), [])
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)

        manager.create_experiment(
            "自定义突破", ["600000"], "breakout_ma_atr", 100000,
            parameters={"ma_period": 45, "breakout_period": 15, "atr_period": 12, "atr_multiplier": 2.5},
        )

        args, kwargs = repository.created[0]
        self.assertEqual(args[8:12], (45, 15, 12, 2.5))
        self.assertEqual(kwargs["short_ma_period"], 5)
        self.assertEqual(kwargs["long_ma_period"], 20)

    def test_defensive_experiment_persists_parameters_and_equal_independent_budgets(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        repository = MemoryPaperRepository()
        manager = PaperTradingManager(
            repository, MemoryPaperMarketData(Quote(
                "600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 100000,
                "测试行情", started, False,
            ), []), paper_settings(), clock=lambda: started,
        )

        session_id = manager.create_experiment(
            "防御策略", ["600000", "600028"], "defensive_mean_reversion", 100000,
            {"trend_ma_period": 100, "max_holding_days": 8, "cooldown_days": 4,
             "target_annual_volatility": 0.12, "max_position_ratio": 0.6},
        )

        session = repository.get_paper_session(session_id)
        rows = repository.get_paper_symbols(session_id)
        args, kwargs = repository.created[-1]
        self.assertEqual([row["budget"] for row in rows], [50000, 50000])
        self.assertEqual(
            [session[key] for key in (
                "trend_ma_period", "max_holding_days", "cooldown_days",
                "target_annual_volatility", "max_position_ratio",
            )],
            [100, 8, 4, 0.12, 0.6],
        )
        self.assertEqual(kwargs["max_holding_days"], 8)
        self.assertEqual(kwargs["cooldown_days"], 4)
        self.assertEqual(kwargs["target_annual_volatility"], 0.12)
        self.assertEqual(kwargs["max_position_ratio"], 0.6)
        self.assertEqual(args[12], [("600000", 50000), ("600028", 50000)])

    def test_defensive_signal_runs_once_per_closed_bar_and_later_quote_only_marks_value(self) -> None:
        # 报价日期为周二、周三；两次查询都以周一收盘日线作出同一个信号。
        monday = date(2026, 10, 12)
        started = datetime(2026, 10, 13, 10, 0, tzinfo=CHINA_TZ)
        quote_one = Quote("600000", "浦发银行", 12, 0, 0, 0, 0, 0, 12, 100, 1200,
                          "测试行情", started + timedelta(minutes=1), False)
        quote_two = Quote(**{**quote_one.__dict__, "latest_price": 13,
                             "fetched_at": started + timedelta(days=1, minutes=1)})
        repository = MemoryPaperRepository()
        repository.session.update({"ends_at": None, "strategy_code": "defensive_mean_reversion"})
        repository.symbol["budget"] = 50000
        signal_bars = make_weekday_bars(monday)
        market_data = MemoryPaperMarketData(
            quote_one, signal_bars, quotes_by_symbol={"600000": quote_one},
        )
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: started)
        session = repository.session
        session.update({"trend_ma_period": 120, "atr_period": 14, "atr_multiplier": 2,
                        "max_holding_days": 5, "cooldown_days": 3,
                        "target_annual_volatility": 0.15, "max_position_ratio": 0.7})

        evaluator_calls = []

        def decide(bars, *args):
            evaluator_calls.append((bars, args))
            return DefensiveDecision("buy", "oversold_mean_reversion", 1.5, 0.7)

        with patch("app.paper_trading.manager.evaluate_defensive_mean_reversion", side_effect=decide):
            manager.run_cycle()

        self.assertEqual(repository.symbol["last_decision_date"], monday)
        self.assertEqual(repository.quote_calls[0]["decision_date"], monday)
        self.assertEqual(repository.quote_calls[0]["trade"]["signal_date"], monday)
        self.assertEqual(evaluator_calls[0][0][-1].trade_date, monday)
        self.assertEqual(repository.trades[0]["execution_price"], 12 * (1 + repository.session["slippage_rate"]))
        self.assertEqual(repository.trades[0]["quantity"], 2900)

        market_data.quotes_by_symbol["600000"] = quote_two
        with patch("app.paper_trading.manager.evaluate_defensive_mean_reversion", side_effect=decide) as evaluator:
            manager.run_cycle()

        self.assertEqual(len(repository.trades), 1)
        self.assertEqual(repository.quote_calls[1]["decision_date"], None)
        self.assertEqual(repository.symbol["last_price"], 13)
        evaluator.assert_not_called()

    def test_defensive_history_cache_separates_400_day_window_from_legacy_window(self) -> None:
        day = date(2026, 10, 14)
        quote = Quote("600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 1000,
                      "测试行情", datetime(2026, 10, 14, 10, 0, tzinfo=CHINA_TZ), False)
        market_data = MemoryPaperMarketData(quote, [])
        manager = PaperTradingManager(MemoryPaperRepository(), market_data, paper_settings())

        manager._history_before("600000", day)
        manager._history_before("600000", day, history_days=400)

        self.assertEqual(len(market_data.get_bars_calls), 2)
        self.assertEqual(market_data.get_bars_calls[0][1], day - timedelta(days=220))
        self.assertEqual(market_data.get_bars_calls[1][1], day - timedelta(days=400))

    def test_defensive_sell_persists_execution_day_and_restart_keeps_cooldown_state(self) -> None:
        monday = date(2026, 10, 12)
        friday = date(2026, 10, 9)
        now = [datetime(2026, 10, 12, 10, 0, tzinfo=CHINA_TZ)]
        quote_mon = Quote("600000", "浦发银行", 9, 0, 0, 0, 0, 0, 9, 100, 900,
                          "测试行情", now[0] + timedelta(minutes=1), False)
        quote_tue = Quote(**{**quote_mon.__dict__, "latest_price": 9.5,
                             "fetched_at": datetime(2026, 10, 13, 10, 1, tzinfo=CHINA_TZ)})
        bars = make_weekday_bars(monday)
        repository = MemoryPaperRepository()
        repository.session.update({"ends_at": None, "strategy_code": "defensive_mean_reversion", "cash": 0})
        repository.symbol.update({"shares": 100, "entry_date": date(2026, 10, 8), "entry_price": 10,
                                  "cost_basis": 1000, "highest_price": 11, "atr_value": 1})
        repository.session.update({"trend_ma_period": 120, "atr_period": 14, "atr_multiplier": 2,
                                   "max_holding_days": 5, "cooldown_days": 3,
                                   "target_annual_volatility": 0.15, "max_position_ratio": 0.7})
        market = MemoryPaperMarketData(quote_mon, bars, quotes_by_symbol={"600000": quote_mon})
        manager = PaperTradingManager(repository, market, paper_settings(), clock=lambda: now[0])
        evaluator_inputs = []

        def sell(bars, *args):
            evaluator_inputs.append(args[3])
            return DefensiveDecision("sell", "mean_reversion")

        with patch("app.paper_trading.manager.evaluate_defensive_mean_reversion", side_effect=sell):
            manager.run_cycle()

        self.assertEqual(repository.symbol["last_exit_date"], monday)
        self.assertEqual(repository.trades[0]["side"], "sell")
        self.assertEqual(repository.trades[0]["signal_date"], friday)

        market.quotes_by_symbol["600000"] = quote_tue
        restarted = PaperTradingManager(repository, market, paper_settings(), clock=lambda: now[0])
        with patch("app.paper_trading.manager.evaluate_defensive_mean_reversion", side_effect=lambda bars, *args: (
            evaluator_inputs.append(args[3]) or DefensiveDecision(None, "cooldown")
        )):
            restarted._process_started_at = datetime(2026, 10, 13, 9, 30, tzinfo=CHINA_TZ)
            restarted.run_cycle()

        self.assertEqual(evaluator_inputs, [None, monday])
        self.assertEqual(repository.symbol["last_decision_date"], monday)

    def test_defensive_duplicate_transaction_rejection_does_not_change_account(self) -> None:
        monday = date(2026, 10, 12)
        now = datetime(2026, 10, 13, 10, 0, tzinfo=CHINA_TZ)
        quote = Quote("600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 1000,
                      "测试行情", now + timedelta(minutes=1), False)
        bars = make_weekday_bars(monday)
        repository = MemoryPaperRepository()
        repository.session.update({"ends_at": None, "strategy_code": "defensive_mean_reversion"})
        repository.symbol["budget"] = 50000
        repository.session.update({"trend_ma_period": 120, "atr_period": 14, "atr_multiplier": 2,
                                   "max_holding_days": 5, "cooldown_days": 3,
                                   "target_annual_volatility": 0.15, "max_position_ratio": 0.7})
        repository.reject_next_quote = True
        manager = PaperTradingManager(repository, MemoryPaperMarketData(quote, bars), paper_settings(), clock=lambda: now)

        with patch("app.paper_trading.manager.evaluate_defensive_mean_reversion",
                   return_value=DefensiveDecision("buy", "oversold_mean_reversion", 1, 0.5)):
            manager.run_cycle()

        self.assertEqual(repository.session["cash"], 100000)
        self.assertEqual(repository.symbol["shares"], 0)
        self.assertEqual(repository.trades, [])

    def test_defensive_no_trade_signal_still_persists_processed_signal_date(self) -> None:
        monday = date(2026, 10, 12)
        now = datetime(2026, 10, 13, 10, 0, tzinfo=CHINA_TZ)
        quote = Quote("600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 1000,
                      "测试行情", now + timedelta(minutes=1), False)
        repository = MemoryPaperRepository()
        repository.session.update({"ends_at": None, "strategy_code": "defensive_mean_reversion"})
        repository.session.update({"trend_ma_period": 120, "atr_period": 14, "atr_multiplier": 2,
                                   "max_holding_days": 5, "cooldown_days": 3,
                                   "target_annual_volatility": 0.15, "max_position_ratio": 0.7})
        manager = PaperTradingManager(
            repository, MemoryPaperMarketData(quote, make_weekday_bars(monday)),
            paper_settings(), clock=lambda: now,
        )

        with patch("app.paper_trading.manager.evaluate_defensive_mean_reversion",
                   return_value=DefensiveDecision(None, "trend_filter_failed")):
            manager.run_cycle()

        self.assertEqual(repository.symbol["last_decision_date"], monday)
        self.assertEqual(repository.trades, [])

    def test_defensive_restarted_worker_rejects_quote_fetched_before_start(self) -> None:
        started = datetime(2026, 10, 13, 9, 30, tzinfo=CHINA_TZ)
        cached = Quote("600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 1000,
                       "测试行情", started - timedelta(seconds=1), False)
        repository = MemoryPaperRepository()
        repository.session.update({"ends_at": None, "strategy_code": "defensive_mean_reversion"})
        manager = PaperTradingManager(repository, MemoryPaperMarketData(cached, []), paper_settings(), clock=lambda: started)

        with patch("app.paper_trading.manager.evaluate_defensive_mean_reversion") as evaluator:
            manager.run_cycle()

        evaluator.assert_not_called()
        self.assertEqual(repository.quote_calls, [])

    def test_create_experiment_rejects_invalid_parameter_relationships(self) -> None:
        repository = MemoryPaperRepository()
        market_data = MemoryPaperMarketData(Quote(
            "600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 100000,
            "AKShare", datetime.now(CHINA_TZ), False,
        ), [])
        manager = PaperTradingManager(repository, market_data, paper_settings())

        with self.assertRaisesRegex(ValueError, "短周期必须小于长周期"):
            manager.create_experiment(
                "无效双均线", ["600000"], "dual_ma", 100000,
                parameters={"short_ma_period": 20, "long_ma_period": 20},
            )

        self.assertEqual(market_data.ensured_symbols, [])
        self.assertEqual(repository.created, [])

    def test_stop_session_changes_only_the_requested_task(self) -> None:
        repository = MemoryPaperRepository()
        repository.add_session("paper-2", 50000, "dual_ma", "600028", 50000)
        manager = PaperTradingManager(repository, MemoryPaperMarketData(
            Quote("600000", "浦发银行", 10, 0, 0, 0, 0, 0, 10, 100, 100000, "AKShare", datetime.now(CHINA_TZ), False),
            [],
        ), paper_settings())

        stopped = manager.stop_session("paper-1")

        self.assertTrue(stopped)
        self.assertEqual(repository.get_paper_session("paper-1")["status"], "stopped")
        self.assertEqual(repository.get_paper_session("paper-2")["status"], "running")

    def test_dual_ma_exit_respects_t_plus_one_and_remains_available_next_day(self) -> None:
        started = datetime(2026, 10, 12, 9, 30, tzinfo=CHINA_TZ)
        today = started.date()
        quote = Quote(
            "600000", "浦发银行", 1, 0, 0, 0, 0, 0, 1, 100, 100000,
            "AKShare", started + timedelta(minutes=1), False,
        )
        repository = MemoryPaperRepository()
        repository.session.update({"ends_at": None, "strategy_code": "dual_ma", "cash": 0})
        repository.symbol.update({
            "shares": 100, "entry_date": today, "entry_price": 5, "cost_basis": 500,
            "highest_price": 5, "last_quote_at": started,
        })
        market_data = MemoryPaperMarketData(quote, make_bars(list(range(20, 0, -1)), high=20, low=1))
        clock = [started]
        manager = PaperTradingManager(repository, market_data, paper_settings(), clock=lambda: clock[0])

        manager.run_cycle()
        self.assertEqual(repository.trades, [])
        self.assertEqual(repository.symbol["shares"], 100)

        clock[0] = started + timedelta(days=1)
        market_data.quote = Quote(**{
            **quote.__dict__, "fetched_at": clock[0] + timedelta(minutes=1),
        })
        manager.run_cycle()

        self.assertEqual(len(repository.trades), 1)
        self.assertEqual(repository.trades[0]["side"], "sell")
        self.assertEqual(repository.symbol["shares"], 0)


if __name__ == "__main__":
    unittest.main()
