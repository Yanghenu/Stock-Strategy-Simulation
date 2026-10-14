import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DatabaseSettings
from app.models import (
    BacktestParams, BacktestResult, Bar, CorporateAction, EquityPoint, Metrics, Trade,
)
from app.repositories.sql_repository import SqlRepository


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.rows: list[tuple[object, ...]] = []
        self.single_row: tuple[object, ...] | None = None
        self.fetchone_queue: list[tuple[object, ...] | None] = []
        self.rowcount = 1
        self.fail_on_sql: str | None = None

    def execute(self, sql: str, parameters: object = None) -> None:
        if self.fail_on_sql and self.fail_on_sql in sql:
            raise RuntimeError("database write failed")
        self.calls.append((sql, parameters))

    def executemany(self, sql: str, parameters: object) -> None:
        self.calls.append((sql, parameters))

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def fetchone(self) -> tuple[object, ...] | None:
        if self.fetchone_queue:
            return self.fetchone_queue.pop(0)
        return self.single_row

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, fail_on_executemany: bool = False) -> None:
        self.cursor_instance = FakeCursor()
        self.fail_on_executemany = fail_on_executemany
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        cursor = self.cursor_instance
        if self.fail_on_executemany:
            cursor.executemany = self.fail  # type: ignore[method-assign]
        return cursor

    def fail(self, sql: str, parameters: object) -> None:
        raise RuntimeError("database write failed")

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class QueryCursor(FakeCursor):
    def __init__(self, run_row: tuple[object, ...], equity: list[tuple[object, ...]], trades: list[tuple[object, ...]], warnings: list[tuple[object, ...]]) -> None:
        super().__init__()
        self.run_row = run_row
        self.equity_rows = equity
        self.trade_rows = trades
        self.warning_rows = warnings
        self.active_rows: list[tuple[object, ...]] = []
        self.active_one: tuple[object, ...] | None = None

    def execute(self, sql: str, parameters: object = None) -> None:
        super().execute(sql, parameters)
        if "FROM dbo.backtest_runs WHERE" in sql:
            self.active_one = self.run_row
        elif "FROM dbo.backtest_equity" in sql:
            self.active_rows = self.equity_rows
        elif "FROM dbo.backtest_run_symbols" in sql:
            self.active_rows = [("600000",)]
        elif "FROM dbo.backtest_trades" in sql:
            self.active_rows = self.trade_rows
        elif "FROM dbo.backtest_warnings" in sql:
            self.active_rows = self.warning_rows

    def fetchone(self) -> tuple[object, ...] | None:
        if self.active_one is not None:
            return self.active_one
        return (1,)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.active_rows


def settings() -> DatabaseSettings:
    return DatabaseSettings(
        server="localhost", database="QuantBacktest", authentication="windows",
        username="", password="", encrypt=True, trust_server_certificate=True,
        connection_timeout_seconds=5, statement_timeout_seconds=30,
    )


def bar() -> Bar:
    return Bar(date(2024, 1, 2), 10, 11, 9, 10, 10, 11, 9, 10, 1000, 10000)


class SqlRepositoryTests(unittest.TestCase):
    def test_defensive_parameters_are_bound_for_backtests_and_paper_sessions(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        params = BacktestParams("600000", date(2024, 1, 1), date(2024, 3, 1), 5, 20, 100000,
                                max_holding_days=8, cooldown_days=4, target_annual_volatility=0.18)
        repository.create_backtest("run-id", params)
        sql, values = connection.cursor_instance.calls[0]
        for column, value in (("max_holding_days", 8), ("cooldown_days", 4), ("target_annual_volatility", 0.18)):
            self.assertIn(column, sql)
            self.assertIn(value, values)
        self.assertEqual(sql.count("?"), len(values))
        connection.cursor_instance.calls.clear()
        repository.create_paper_session("paper-id", datetime(2026, 10, 12), None, 100000,
                                        0.0003, 5, 0.001, 0.0005, 60, 20, 20, 2, [("600000", 100000)],
                                        max_holding_days=8, cooldown_days=4,
                                        target_annual_volatility=0.18, max_position_ratio=0.6)
        sql, values = connection.cursor_instance.calls[0]
        self.assertIn("max_position_ratio", sql)
        self.assertEqual(values[-4:], (8, 4, 0.18, 0.6))
        self.assertEqual(sql.count("?"), len(values))

    def test_duplicate_or_older_signal_date_rejects_all_writes_under_lock(self) -> None:
        # 行情时间更新不能绕过信号日期防重，拒绝后现金、持仓及成交都不得变化。
        for decision in (date(2026, 10, 12), date(2026, 10, 11)):
            with self.subTest(decision=decision):
                connection = FakeConnection()
                connection.cursor_instance.fetchone_queue = [("running", None), (None, date(2026, 10, 12))]
                repository = SqlRepository(settings(), connection_factory=lambda: connection)
                applied = repository.apply_paper_quote("paper-id", "600000", datetime(2026, 10, 13),
                                                       {}, 0, {}, decision_date=decision)
                self.assertFalse(applied)
                self.assertFalse(any("UPDATE" in sql or "INSERT" in sql for sql, _ in connection.cursor_instance.calls))
                self.assertIn("UPDLOCK, HOLDLOCK", connection.cursor_instance.calls[1][0])

    def test_new_signal_date_is_saved_even_without_a_trade(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.fetchone_queue = [("running", None), (None, None)]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        state = {"shares": 0, "entry_date": None, "entry_price": None, "cost_basis": 0,
                 "highest_price": None, "atr_value": None, "last_price": 10,
                 "last_exit_date": date(2026, 10, 9)}
        self.assertTrue(repository.apply_paper_quote("paper-id", "600000", datetime(2026, 10, 13),
                                                     state, 100000, None, decision_date=date(2026, 10, 12)))
        sql, values = next(call for call in connection.cursor_instance.calls if "UPDATE dbo.paper_session_symbols" in call[0])
        self.assertIn("last_decision_date", sql)
        self.assertIn(date(2026, 10, 12), values)
        self.assertIn(date(2026, 10, 9), values)
        self.assertEqual(sql.count("?"), len(values))
        self.assertFalse(any("INSERT INTO dbo.paper_trades" in sql for sql, _ in connection.cursor_instance.calls))

    def test_signal_date_trade_and_old_path_dates_use_bound_parameters(self) -> None:
        for decision in (date(2026, 10, 12), None):
            with self.subTest(decision=decision):
                connection = FakeConnection()
                connection.cursor_instance.fetchone_queue = [("running", None), (None, date(2026, 10, 9))]
                repository = SqlRepository(settings(), connection_factory=lambda: connection)
                state = {"shares": 100, "entry_date": date(2026, 10, 13), "entry_price": 10,
                         "cost_basis": 1005, "highest_price": 10, "atr_value": None, "last_price": 10}
                trade = {"side": "buy", "execution_price": 10, "quantity": 100,
                         "notional": 1000, "commission": 5, "stamp_duty": 0, "reason": "防御买入"}
                self.assertTrue(repository.apply_paper_quote("paper-id", "600000", datetime(2026, 10, 13),
                                                             state, 98995, trade, decision_date=decision))
                sql, values = next(call for call in connection.cursor_instance.calls if "INSERT INTO dbo.paper_trades" in call[0])
                self.assertIn("signal_date", sql)
                self.assertEqual(values[-1], decision)
                self.assertEqual(sql.count("?"), len(values))
                update_sql, update_values = next(call for call in connection.cursor_instance.calls if "UPDATE dbo.paper_session_symbols" in call[0])
                self.assertIn("last_exit_date=COALESCE", update_sql)
                self.assertIsNone(update_values[8])
                self.assertEqual(update_values[9], decision)
                self.assertTrue(connection.committed)
                if decision is not None:
                    # 第二次报价更晚，但数据库已保存同一信号日；只能保留第一笔成交和现金写入。
                    connection.cursor_instance.fetchone_queue = [
                        ("running", None), (datetime(2026, 10, 13), decision),
                    ]
                    self.assertFalse(repository.apply_paper_quote("paper-id", "600000", datetime(2026, 10, 14),
                                                                  state, 97990, trade, decision_date=decision))
                    self.assertEqual(sum("INSERT INTO dbo.paper_trades" in sql for sql, _ in connection.cursor_instance.calls), 1)
                    self.assertEqual(sum("UPDATE dbo.paper_sessions SET cash" in sql for sql, _ in connection.cursor_instance.calls), 1)

    def test_paper_symbol_date_columns_are_mapped_with_nullable_legacy_rows(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.rows = [
            ("600000", "浦发银行", 100000, 0, None, None, 0, None, None, 10, None,
             date(2026, 10, 9), date(2026, 10, 12)),
            ("600028", "中国石化", 100000, 0, None, None, 0, None, None, 6, None, None, None),
        ]
        rows = SqlRepository(settings(), connection_factory=lambda: connection).get_paper_symbols("paper-id")
        self.assertEqual(rows[0]["last_exit_date"], date(2026, 10, 9))
        self.assertEqual(rows[0]["last_decision_date"], date(2026, 10, 12))
        self.assertIsNone(rows[1]["last_exit_date"])
        self.assertIsNone(rows[1]["last_decision_date"])

    def test_paper_detail_maps_parameters_symbol_dates_and_trade_signal(self) -> None:
        class DetailCursor(FakeCursor):
            def execute(self, sql: str, parameters: object = None) -> None:
                super().execute(sql, parameters)
                if "FROM dbo.paper_sessions WHERE" in sql:
                    self.single_row = ("paper-id", "防御任务", "defensive_mean_reversion", "running",
                                       datetime(2026, 10, 12), None, None, 100000, 100000,
                                       60, 20, 20, 2, 0.0003, 5, 0.001, 0.0005, 5, 20, 20, 60, 3, 7,
                                       None, None, 8, 4, 0.18, 0.6)
                elif "FROM dbo.paper_session_symbols" in sql:
                    self.rows = [("600000", "浦发银行", 100000, 0, None, None, 0, None, None, 10,
                                  datetime(2026, 10, 13), date(2026, 10, 9), date(2026, 10, 12))]
                elif "FROM dbo.paper_equity" in sql:
                    self.rows = []
                elif "FROM dbo.paper_trades" in sql:
                    self.rows = [("600000", datetime(2026, 10, 13), "buy", 10, 100, 1000, 5, 0,
                                  "防御买入", date(2026, 10, 12))]

        connection = FakeConnection()
        connection.cursor_instance = DetailCursor()
        detail = SqlRepository(settings(), connection_factory=lambda: connection).get_paper_session("paper-id")
        self.assertEqual(detail["max_holding_days"], 8)
        self.assertEqual(detail["cooldown_days"], 4)
        self.assertEqual(detail["target_annual_volatility"], 0.18)
        self.assertEqual(detail["max_position_ratio"], 0.6)
        self.assertEqual(detail["symbols"][0]["last_exit_date"], date(2026, 10, 9))
        self.assertEqual(detail["symbols"][0]["last_decision_date"], date(2026, 10, 12))
        self.assertEqual(detail["trades"][0]["signal_date"], date(2026, 10, 12))

    def test_signal_day_trade_failure_rolls_back_position_cash_and_decision(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.fetchone_queue = [("running", None), (None, None)]
        connection.cursor_instance.fail_on_sql = "INSERT INTO dbo.paper_trades"
        state = {"shares": 100, "entry_date": date(2026, 10, 13), "entry_price": 10,
                 "cost_basis": 1005, "highest_price": 10, "atr_value": None, "last_price": 10}
        trade = {"side": "buy", "execution_price": 10, "quantity": 100,
                 "notional": 1000, "commission": 5, "stamp_duty": 0, "reason": "防御买入"}
        with self.assertLogs("app.repositories.sql_repository", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "database write failed"):
                SqlRepository(settings(), connection_factory=lambda: connection).apply_paper_quote(
                    "paper-id", "600000", datetime(2026, 10, 13), state, 98995, trade,
                    decision_date=date(2026, 10, 12))
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.closed)

    def test_creates_run_with_bound_strategy_and_fee_parameters(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        backtest_params = BacktestParams(
            "600000", date(2024, 1, 1), date(2024, 3, 1), 5, 20,
            100000, 1, 0.0003, 5, 0.001, 0.0005,
        )

        repository.create_backtest("run-id", backtest_params)

        sql, values = connection.cursor_instance.calls[0]
        self.assertIn("dbo.backtest_runs", sql)
        self.assertNotIn("600000", sql)
        self.assertIn("600000", values)  # type: ignore[operator]
        self.assertEqual(sql.count("?"), len(values))  # type: ignore[arg-type]
        symbol_rows = next(rows for query, rows in connection.cursor_instance.calls if "INSERT INTO dbo.backtest_run_symbols" in query)
        self.assertEqual(symbol_rows, [("run-id", "600000", 0)])
        self.assertTrue(connection.committed)

    def test_creates_multi_stock_run_with_scalar_strategy_parameters(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        backtest_params = BacktestParams(
            "600000", date(2024, 1, 1), date(2024, 3, 1), 5, 20, 100000,
            strategy_code="momentum_rotation", symbols=("600000", "000333"),
            momentum_period_days=15, trend_ma_period=45, max_positions=2,
        )

        repository.create_backtest("run-id", backtest_params)

        sql, values = connection.cursor_instance.calls[0]
        self.assertIn("momentum_period_days", sql)
        self.assertIn("momentum_rotation", values)
        self.assertIn(45, values)
        self.assertEqual(connection.cursor_instance.calls[1][1], [
            ("run-id", "600000", 0), ("run-id", "000333", 1),
        ])

    def test_creates_normalized_paper_session_and_symbol_rows(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        repository.create_paper_session(
            "paper-id", datetime(2026, 10, 12), datetime(2026, 11, 12), 100000,
            0.0003, 5, 0.001, 0.0005, 60, 20, 20, 2.0,
            [("600000", 50000), ("600028", 50000)],
            task_name="银行策略对比", strategy_code="dual_ma", short_ma_period=5, long_ma_period=20,
            momentum_period_days=20, trend_ma_period=60, max_positions=3, rebalance_interval_days=7,
        )

        sql, values = connection.cursor_instance.calls[0]
        self.assertIn("dbo.paper_sessions", sql)
        self.assertIn("task_name", sql)
        self.assertIn("strategy_code", sql)
        # 参数占位符必须与绑定值逐一对应，避免 SQL Server 创建任务时才报列数不匹配。
        self.assertEqual(sql.count("?"), len(values))  # type: ignore[arg-type]
        self.assertNotIn("JSON", sql.upper())
        self.assertEqual(values[0], "paper-id")  # type: ignore[index]
        self.assertIn("银行策略对比", values)  # type: ignore[operator]
        self.assertIn("dual_ma", values)  # type: ignore[operator]
        self.assertIn(20, values)  # type: ignore[operator]
        self.assertIn(60, values)  # type: ignore[operator]
        self.assertIn(3, values)  # type: ignore[operator]
        self.assertIn(7, values)  # type: ignore[operator]
        symbols_sql, symbols = connection.cursor_instance.calls[1]
        self.assertIn("dbo.paper_session_symbols", symbols_sql)
        self.assertEqual(len(symbols), 2)  # type: ignore[arg-type]
        equity_sql, _ = connection.cursor_instance.calls[2]
        self.assertIn("dbo.paper_equity", equity_sql)
        self.assertTrue(connection.committed)

    def test_paper_quote_update_is_idempotent_by_source_timestamp(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.fetchone_queue = [("running", None), (None, None)]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        quote_time = datetime(2026, 10, 12, 6, 55)
        state = {
            "shares": 0, "entry_date": None, "entry_price": None, "cost_basis": 0,
            "highest_price": None, "atr_value": None, "last_price": 9.65,
        }

        applied = repository.apply_paper_quote(
            "paper-id", "600000", quote_time, state, 100000, None,
        )

        self.assertTrue(applied)
        updates = [sql for sql, _ in connection.cursor_instance.calls]
        self.assertTrue(any("last_quote_at < ?" in sql for sql in updates))
        self.assertTrue(connection.committed)

        duplicate_cursor = FakeCursor()
        duplicate_cursor.fetchone_queue = [("running", None), (quote_time, None)]
        duplicate_connection = FakeConnection()
        duplicate_connection.cursor_instance = duplicate_cursor
        duplicate_repository = SqlRepository(settings(), connection_factory=lambda: duplicate_connection)
        applied = duplicate_repository.apply_paper_quote(
            "paper-id", "600000", quote_time, state, 100000, None,
        )
        self.assertFalse(applied)
        self.assertFalse(any("UPDATE dbo.paper_session_symbols" in sql for sql, _ in duplicate_cursor.calls))

    def test_paper_rebalance_commits_all_symbols_trades_cash_and_equity_together(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.rows = [("600000", None), ("600028", None)]
        connection.cursor_instance.fetchone_queue = [("running", None, 100000), (100000,)]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        quote_time = datetime(2026, 10, 12, 10, 0)
        updates = [
            {
                "symbol": symbol, "budget": budget, "shares": shares, "entry_date": entry_date,
                "entry_price": entry_price, "cost_basis": cost_basis, "highest_price": highest,
                "atr_value": None, "last_price": 10.0, "last_quote_at": quote_time,
            }
            for symbol, budget, shares, entry_date, entry_price, cost_basis, highest in (
                ("600000", 50000, 100, date(2026, 10, 12), 10.0, 1005, 10.0),
                ("600028", 50000, 0, None, None, 0, None),
            )
        ]
        trades = [{
            "symbol": "600000", "quote_fetched_at": quote_time, "side": "buy",
            "execution_price": 10.0, "quantity": 100, "notional": 1000,
            "commission": 5, "stamp_duty": 0, "reason": "强弱轮动买入",
            "signal_date": date(2026, 10, 9),
        }]

        applied = repository.apply_paper_rebalance(
            "paper-id", quote_time, 98995, updates, trades, quote_time,
        )

        self.assertTrue(applied)
        sql_calls = [sql for sql, _ in connection.cursor_instance.calls]
        self.assertTrue(any("UPDATE dbo.paper_session_symbols" in sql for sql in sql_calls))
        self.assertTrue(any("INSERT INTO dbo.paper_trades" in sql for sql in sql_calls))
        self.assertTrue(any("last_rebalanced_at=COALESCE" in sql for sql in sql_calls))
        self.assertTrue(any("MERGE dbo.paper_equity" in sql for sql in sql_calls))
        trade_sql, trade_values = next(call for call in connection.cursor_instance.calls if "INSERT INTO dbo.paper_trades" in call[0])
        self.assertEqual(trade_values[-1], date(2026, 10, 9))
        self.assertEqual(trade_sql.count("?"), len(trade_values))
        update_sql, update_values = next(call for call in connection.cursor_instance.calls if "UPDATE dbo.paper_session_symbols" in call[0])
        self.assertIn("last_decision_date=COALESCE", update_sql)
        self.assertIsNone(update_values[9])
        self.assertIsNone(update_values[10])
        self.assertEqual(update_sql.count("?"), len(update_values))
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)

    def test_paper_rebalance_rolls_back_all_writes_when_batch_update_fails(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.rows = [("600000", None)]
        connection.cursor_instance.fetchone_queue = [("running", None, 100000)]
        connection.cursor_instance.fail_on_sql = "INSERT INTO dbo.paper_trades"
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        quote_time = datetime(2026, 10, 12, 10, 0)
        update = {
            "symbol": "600000", "budget": 100000, "shares": 0, "entry_date": None,
            "entry_price": None, "cost_basis": 0, "highest_price": None, "atr_value": None,
            "last_price": 10.0, "last_quote_at": quote_time,
        }

        with self.assertRaisesRegex(RuntimeError, "database write failed"):
            repository.apply_paper_rebalance(
                "paper-id", quote_time, 98995, [update], [{
                    "symbol": "600000", "quote_fetched_at": quote_time, "side": "buy",
                    "execution_price": 10.0, "quantity": 100, "notional": 1000,
                    "commission": 5, "stamp_duty": 0, "reason": "轮动买入",
                }], quote_time,
            )

        self.assertTrue(any("UPDATE dbo.paper_session_symbols" in sql for sql, _ in connection.cursor_instance.calls))
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)

    def test_paper_session_detail_returns_rotation_parameters_and_last_rebalance(self) -> None:
        connection = FakeConnection()
        last_rebalanced_at = datetime(2026, 10, 12, 10, 0)
        connection.cursor_instance.fetchone_queue = [(
            "paper-id", "强弱轮动组合实验", "momentum_rotation", "running", datetime(2026, 10, 12), None,
            None, 100000, 60000, 60, 20, 20, 2.0, 0.0003, 5, 0.001, 0.0005,
            5, 20, 20, 60, 3, 7, last_rebalanced_at, None,
        )]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        session = repository.get_paper_session("paper-id")

        self.assertEqual(session["momentum_period_days"], 20)
        self.assertEqual(session["trend_ma_period"], 60)
        self.assertEqual(session["max_positions"], 3)
        self.assertEqual(session["rebalance_interval_days"], 7)
        self.assertEqual(session["last_rebalanced_at"], last_rebalanced_at)

    def test_paper_quote_is_ignored_after_session_was_stopped(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.fetchone_queue = [("stopped", None)]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        state = {
            "shares": 0, "entry_date": None, "entry_price": None, "cost_basis": 0,
            "highest_price": None, "atr_value": None, "last_price": 9.65,
        }

        applied = repository.apply_paper_quote(
            "paper-id", "600000", datetime(2026, 10, 12, 6, 55), state, 100000, None,
        )

        self.assertFalse(applied)
        self.assertEqual(len(connection.cursor_instance.calls), 1)
        self.assertTrue(connection.committed)

    def test_paper_quote_completes_monthly_task_when_processing_reaches_expiry(self) -> None:
        connection = FakeConnection()
        ends_at = datetime(2026, 10, 12, 9, 30)
        connection.cursor_instance.fetchone_queue = [("running", ends_at)]
        repository = SqlRepository(
            settings(), connection_factory=lambda: connection, clock=lambda: ends_at,
        )
        state = {
            "shares": 0, "entry_date": None, "entry_price": None, "cost_basis": 0,
            "highest_price": None, "atr_value": None, "last_price": 9.65,
        }

        applied = repository.apply_paper_quote(
            "paper-id", "600000", ends_at, state, 100000, None, processed_at=ends_at,
        )

        self.assertFalse(applied)
        self.assertEqual(len(connection.cursor_instance.calls), 2)
        self.assertIn("status='completed'", connection.cursor_instance.calls[1][0])
        self.assertTrue(connection.committed)

    def test_paper_quote_checks_clock_after_acquiring_session_lock(self) -> None:
        connection = FakeConnection()
        ends_at = datetime(2026, 10, 12, 9, 30)
        connection.cursor_instance.fetchone_queue = [("running", ends_at)]
        repository = SqlRepository(
            settings(), connection_factory=lambda: connection,
            clock=lambda: ends_at,
        )
        state = {
            "shares": 0, "entry_date": None, "entry_price": None, "cost_basis": 0,
            "highest_price": None, "atr_value": None, "last_price": 9.65,
        }

        applied = repository.apply_paper_quote(
            "paper-id", "600000", ends_at, state, 100000, None,
            processed_at=ends_at.replace(minute=29),
        )

        self.assertFalse(applied)
        self.assertEqual(len(connection.cursor_instance.calls), 2)
        self.assertIn("status='completed'", connection.cursor_instance.calls[1][0])

    def test_database_timestamps_are_normalized_to_china_time(self) -> None:
        utc_value = datetime(2026, 10, 12, 1, 30, tzinfo=timezone.utc)

        self.assertEqual(
            SqlRepository._db_datetime(utc_value), datetime(2026, 10, 12, 9, 30),
        )

    def test_reads_all_running_paper_sessions_with_nullable_expiry(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.rows = [
            (
                "paper-id", "多任务", "dual_ma", datetime(2026, 10, 12), None, 100000, 90000,
                0.0003, 5, 0.001, 0.0005, 60, 20, 20, 2.0, 5, 20, 20, 60, 3, 7, None,
                8, 4, 0.18, 0.6,
            ),
        ]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        sessions = repository.get_active_paper_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "paper-id")
        self.assertIsNone(sessions[0]["ends_at"])
        self.assertEqual(sessions[0]["strategy_code"], "dual_ma")
        self.assertEqual(sessions[0]["momentum_period_days"], 20)
        self.assertEqual(sessions[0]["trend_ma_period"], 60)
        self.assertEqual(sessions[0]["max_positions"], 3)
        self.assertEqual(sessions[0]["rebalance_interval_days"], 7)
        self.assertIsNone(sessions[0]["last_rebalanced_at"])
        self.assertEqual(sessions[0]["max_holding_days"], 8)
        self.assertEqual(sessions[0]["cooldown_days"], 4)
        self.assertEqual(sessions[0]["target_annual_volatility"], 0.18)
        self.assertEqual(sessions[0]["max_position_ratio"], 0.6)
        sql, _ = connection.cursor_instance.calls[0]
        self.assertNotIn("TOP (1)", sql)

    def test_lists_paper_session_summary_without_per_session_queries(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.rows = [
            (
                "paper-id", "多任务", "dual_ma", "running", datetime(2026, 10, 12), None, None,
                100000, 90000, 10000, 100000, 0, -0.05, 2, None,
            ),
        ]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        sessions = repository.list_paper_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["task_name"], "多任务")
        self.assertEqual(sessions[0]["equity"], 100000)
        self.assertEqual(sessions[0]["symbol_count"], 2)
        self.assertEqual(len(connection.cursor_instance.calls), 1)

    def test_stopping_a_session_only_updates_running_status(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        stopped = repository.stop_paper_session("paper-id", datetime(2026, 10, 12, 15))

        self.assertTrue(stopped)
        sql, values = connection.cursor_instance.calls[0]
        self.assertIn("status='stopped'", sql)
        self.assertIn("status='running'", sql)
        self.assertEqual(values[1], "paper-id")  # type: ignore[index]
        self.assertTrue(connection.committed)

    def test_saves_run_summary_curve_and_trades_in_one_transaction(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        result = BacktestResult(
            symbol="600000",
            metrics=Metrics(0.1, 0.2, -0.05, 0.12, 1.2, 1.0, 1),
            trades=[
                Trade(date(2024, 1, 2), date(2024, 1, 1), "buy", 10, 100, 1000, 5, 0, "signal"),
                Trade(date(2024, 1, 3), date(2024, 1, 2), "sell", 11, 100, 1100, 5, 1.1, "signal"),
            ],
            equity_curve=[EquityPoint(date(2024, 1, 2), 1000, 100, 1000, 2000, 0, 2000)],
            fees_total=5,
            dividends_total=0,
            benchmark_return=0.05,
            warnings=["未确认日期的权益事件未计入"],
        )

        repository.save_backtest_result("run-id", result)

        self.assertTrue(connection.committed)
        self.assertEqual(sum(1 for sql, _ in connection.cursor_instance.calls if "INSERT INTO dbo.backtest_" in sql), 3)
        self.assertEqual(len(connection.cursor_instance.calls), 7)
        trade_rows = next(
            rows for sql, rows in connection.cursor_instance.calls if "INSERT INTO dbo.backtest_trades" in sql
        )
        trade_sql = next(sql for sql, _ in connection.cursor_instance.calls if "INSERT INTO dbo.backtest_trades" in sql)
        self.assertEqual(trade_sql.count("?"), len(trade_rows[0]))
        self.assertEqual(trade_rows[0][4], "600000")
        self.assertTrue(all(type(row[9]) is float for row in trade_rows))  # 印花税列需要保持统一的浮点类型。

    def test_cancel_update_is_limited_to_queued_or_running_runs(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        self.assertTrue(repository.request_cancel("run-id"))

        sql, values = connection.cursor_instance.calls[0]
        self.assertIn("status IN ('queued', 'running')", sql)
        self.assertEqual(values, ("run-id",))

    def test_loads_persisted_run_curve_trades_and_warnings(self) -> None:
        run_row = (
            "run-id", "600000", date(2024, 1, 1), date(2024, 1, 31), "dual_moving_average",
            5, 20, 60, 20, 20, 2, 20, 60, 3, 7,
            100000, 1, 0.0003, 5, 0.001, 0.0005, "completed", 100,
            0.1, 0.2, -0.05, 0.12, 1.2, 1.0, 1, 5, 0, 0.05, None, date(2024, 1, 1), 8, 4, 0.18,
        )
        cursor = QueryCursor(
            run_row,
            [(date(2024, 1, 2), 1000, 100, 1000, 2000, 0, 2000)],
            [(date(2024, 1, 2), date(2024, 1, 1), "buy", "600000", 10, 100, 1000, 5, 0, "signal")],
            [("权益事件提示",)],
        )
        connection = FakeConnection()
        connection.cursor_instance = cursor
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        record = repository.get_backtest("run-id")

        self.assertEqual(record["status"], "completed")  # type: ignore[index]
        self.assertEqual(record["max_holding_days"], 8)
        self.assertEqual(record["cooldown_days"], 4)
        self.assertEqual(record["target_annual_volatility"], 0.18)
        self.assertEqual(len(record["equity_curve"]), 1)  # type: ignore[index]
        self.assertEqual(record["trades"][0]["side"], "buy")  # type: ignore[index]
        self.assertEqual(record["trades"][0]["symbol"], "600000")  # type: ignore[index]
        self.assertEqual(record["symbols"], ["600000"])  # type: ignore[index]
        self.assertEqual(record["warnings"], ["权益事件提示"])  # type: ignore[index]

    def test_polling_running_backtest_skips_empty_curve_and_trade_queries(self) -> None:
        row = (
            "run-id", "600000", date(2024, 1, 1), date(2024, 1, 31), "dual_ma",
            5, 20, 60, 20, 20, 2, 20, 60, 3, 7,
            100000, 1, 0.0003, 5, 0.001, 0.0005, "running", 35,
            None, None, None, None, None, None, None, None, None, None, None,
            None, date(2024, 1, 1), 5, 3, 0.15,
        )
        cursor = QueryCursor(row, [], [], [])
        connection = FakeConnection()
        connection.cursor_instance = cursor
        record = SqlRepository(settings(), connection_factory=lambda: connection).get_backtest("run-id")

        self.assertEqual(record["status"], "running")  # type: ignore[index]
        self.assertEqual(record["trades"], [])  # type: ignore[index]
        self.assertEqual(len(cursor.calls), 2)

    def test_upserts_stock_name_with_bound_parameters(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        repository.upsert_stock("600000", "浦发银行", "SH")

        sql, parameters = connection.cursor_instance.calls[0]
        self.assertIn("dbo.stocks", sql)
        self.assertNotIn("浦发银行", sql)
        self.assertEqual(parameters, ("600000", "浦发银行", "SH"))
        self.assertTrue(connection.committed)

    def test_upserts_rows_with_parameterized_batch_and_commits(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        repository.upsert_bars("600000", [bar()])

        sql, rows = connection.cursor_instance.calls[0]
        self.assertIn("daily_bars", sql)
        self.assertIn("?", sql)
        self.assertNotIn("600000", sql)
        self.assertEqual(len(rows), 1)  # type: ignore[arg-type]
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)

    def test_rolls_back_and_closes_connection_after_write_error(self) -> None:
        connection = FakeConnection(fail_on_executemany=True)
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        with self.assertLogs("app.repositories.sql_repository", level="ERROR"):
            with self.assertRaises(RuntimeError):
                repository.upsert_bars("600000", [bar()])

        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertFalse(connection.committed)

    def test_reads_rows_into_standardized_bars(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.rows = [
            (date(2024, 1, 2), 10, 11, 9, 10, 10, 11, 9, 10, 1000, 10000)
        ]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        bars = repository.get_bars("600000", date(2024, 1, 1), date(2024, 1, 3))

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].trade_date, date(2024, 1, 2))
        self.assertEqual(bars[0].adj_close, 10)
        self.assertNotIn("600000", connection.cursor_instance.calls[0][0])

    def test_bar_coverage_is_checked_and_saved_as_normalized_dates(self) -> None:
        query_connection = FakeConnection()
        query_connection.cursor_instance.single_row = (1,)
        save_connection = FakeConnection()
        connections = iter((query_connection, save_connection))
        repository = SqlRepository(settings(), connection_factory=lambda: next(connections))

        self.assertTrue(repository.has_bar_coverage("600000", date(2024, 1, 1), date(2024, 1, 31)))
        repository.mark_bar_coverage("600000", date(2024, 1, 1), date(2024, 1, 31))

        self.assertIn("daily_bar_coverage", query_connection.cursor_instance.calls[0][0])
        self.assertEqual(query_connection.cursor_instance.calls[0][1], ("600000", date(2024, 1, 1), date(2024, 1, 31)))
        self.assertTrue(save_connection.committed)

    def test_upserts_corporate_actions_as_individual_columns(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        action = CorporateAction("600000", date(2024, 1, 5), date(2024, 1, 8), 0.3, 0.1)

        repository.upsert_actions("600000", [action])

        sql, rows = connection.cursor_instance.calls[0]
        self.assertIn("dbo.corporate_actions", sql)
        self.assertIn("?", sql)
        self.assertNotIn("JSON", sql.upper())
        self.assertEqual(len(rows), 1)  # type: ignore[arg-type]

    def test_upsert_actions_keeps_zero_amounts_float_typed_in_batch(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)
        actions = [
            CorporateAction("600000", date(2024, 1, 5), date(2024, 1, 8), 0, 0),
            CorporateAction("600000", date(2024, 2, 5), date(2024, 2, 8), 0.3, 0.1),
        ]

        repository.upsert_actions("600000", actions)

        _, rows = connection.cursor_instance.calls[0]
        values = rows  # type: ignore[assignment]
        self.assertTrue(all(type(row[4]) is float and type(row[5]) is float for row in values))

    def test_reads_unresolved_actions_so_backtest_can_report_them(self) -> None:
        connection = FakeConnection()
        connection.cursor_instance.rows = [
            (date(2024, 1, 5), date(2024, 1, 8), 0.2, 0.1, "implemented", "done"),
            (date(2024, 1, 5), None, 0.2, 0, "unresolved", "pending"),
        ]
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        actions = repository.get_actions("600000", date(2024, 1, 1), date(2024, 1, 31))

        self.assertIsNone(actions[1].ex_date)
        self.assertEqual(actions[1].status, "unresolved")
        self.assertIn("OR ex_date IS NULL", connection.cursor_instance.calls[0][0])

    def test_watchlist_uses_bound_values_and_supports_listing_and_removal(self) -> None:
        connection = FakeConnection()
        repository = SqlRepository(settings(), connection_factory=lambda: connection)

        repository.add_watchlist("600000", "浦发银行")
        add_sql, add_values = connection.cursor_instance.calls[0]
        self.assertIn("dbo.market_watchlist", add_sql)
        self.assertNotIn("浦发银行", add_sql)
        self.assertIn("浦发银行", add_values)  # type: ignore[operator]
        self.assertEqual(add_sql.count("?"), len(add_values))  # type: ignore[arg-type]
        self.assertNotIn("JSON", add_sql.upper())

        connection.cursor_instance.rows = [("600000", "浦发银行")]
        watched = repository.list_watchlist()
        self.assertEqual((watched[0].symbol, watched[0].name), ("600000", "浦发银行"))
        self.assertTrue(repository.remove_watchlist("600000"))
        self.assertIn("DELETE FROM dbo.market_watchlist", connection.cursor_instance.calls[-1][0])


if __name__ == "__main__":
    unittest.main()
