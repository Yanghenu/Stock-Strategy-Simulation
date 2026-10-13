import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DatabaseSettings
from app.fund_data.models import FundBar, FundDistribution, FundInstrument, FundNavPoint
from app.repositories.fund_repository import FundRepository


class Cursor:
    def __init__(self):
        self.calls = []
        self.rows = []
        self.rowcount = 1
        self.closed = False
        self.fail = False

    def execute(self, sql, values=None):
        if self.fail:
            raise RuntimeError("write failed")
        self.calls.append((sql, values))

    def executemany(self, sql, rows):
        if self.fail:
            raise RuntimeError("write failed")
        self.calls.append((sql, list(rows)))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self.db_cursor = Cursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.db_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def settings():
    return DatabaseSettings("localhost", "QuantBacktest", "sql", "u", "p", True, True, 5, 20)


class FundRepositoryTests(unittest.TestCase):
    def test_fund_type_and_code_are_both_bound_when_upserting_and_searching(self):
        conn = Connection()
        repo = FundRepository(settings(), connection_factory=lambda: conn)
        repo.upsert_instruments([FundInstrument("510300", "沪深300ETF", "etf")])
        repo.search_funds("5103", "etf")

        upsert_sql, upsert_values = conn.db_cursor.calls[0]
        search_sql, search_values = conn.db_cursor.calls[1]
        self.assertIn("source (fund_type, symbol", upsert_sql)
        self.assertNotIn("510300", upsert_sql)
        self.assertIn("510300", upsert_values)
        self.assertIn("AND fund_type=?", search_sql)
        self.assertEqual(search_values[-1], "etf")

    def test_repeated_history_upsert_uses_composite_merge_key(self):
        conn = Connection()
        repo = FundRepository(settings(), connection_factory=lambda: conn)
        bars = [FundBar(date(2026, 10, 12), 1, 1.1, 0.9, 1.05, 1000, 2000)]

        repo.upsert_bars("etf", "510300", bars)
        repo.upsert_bars("etf", "510300", bars)

        self.assertEqual(len(conn.db_cursor.calls), 2)
        self.assertTrue(all("MERGE dbo.fund_daily_bars" in call[0] for call in conn.db_cursor.calls))
        self.assertTrue(all("target.fund_type=source.fund_type" in call[0] for call in conn.db_cursor.calls))

    def test_failed_write_rolls_back_and_closes_cursor_and_connection(self):
        conn = Connection()
        conn.db_cursor.fail = True
        repo = FundRepository(settings(), connection_factory=lambda: conn)

        with self.assertRaisesRegex(RuntimeError, "write failed"):
            repo.upsert_nav_history("open_end", "000001", [FundNavPoint(date(2026, 10, 12), 1.2, 2.0, 0.01)])

        self.assertTrue(conn.rolled_back)
        self.assertTrue(conn.closed)
        self.assertTrue(conn.db_cursor.closed)

    def test_distribution_history_is_read_as_structured_rows(self):
        conn = Connection()
        conn.db_cursor.rows = [(date(2026, 10, 12), 0.005, "source-1")]
        repo = FundRepository(settings(), connection_factory=lambda: conn)

        result = repo.get_distributions("open_end", "000001")

        self.assertEqual(result, [FundDistribution(date(2026, 10, 12), 0.005, "source-1")])
        sql, values = conn.db_cursor.calls[0]
        self.assertIn("FROM dbo.fund_distributions", sql)
        self.assertEqual(values, ("open_end", "000001"))

    def test_low_volatility_window_is_inserted_as_a_structured_session_column(self):
        conn = Connection()
        repo = FundRepository(settings(), connection_factory=lambda: conn)
        payload = {
            "task_name": "低波动组合", "fund_type": "etf", "strategy_code": "fund_low_volatility",
            "started_at": datetime(2026, 10, 13),
            "initial_capital": 10000, "commission_rate": 0.0003, "minimum_commission": 5,
            "stamp_duty_rate": 0, "slippage_rate": 0, "subscription_fee_rate": 0,
            "redemption_fee_rate": 0, "ma_period": 60, "breakout_period": 20,
            "atr_period": 20, "atr_multiplier": 2, "short_ma_period": 5, "long_ma_period": 20,
            "momentum_period_days": 20, "trend_ma_period": 60, "max_positions": 3,
            "rebalance_interval_days": 7, "risk_lookback_days": 45,
            "funds": [{"symbol": "510300", "name": "沪深300ETF"}],
        }

        repo.create_session("session-id", payload)

        insert_sql, values = conn.db_cursor.calls[0]
        columns = insert_sql.split("(", 1)[1].split(")", 1)[0].split(", ")
        self.assertIn("risk_lookback_days", columns)
        self.assertEqual(values[columns.index("risk_lookback_days")], 45)

    def test_delete_stopped_session_removes_dependent_rows_in_one_transaction(self):
        conn = Connection()
        conn.db_cursor.rows = [("stopped",)]
        repo = FundRepository(settings(), connection_factory=lambda: conn)
        delete_session = getattr(repo, "delete_session", None)

        self.assertTrue(callable(delete_session), "基金仓储需要提供已停止任务删除操作")
        if not callable(delete_session):
            return
        deleted = delete_session("session-id")

        self.assertTrue(deleted)
        statements = [sql for sql, _ in conn.db_cursor.calls]
        delete_tables = [next(table for table in (
            "fund_paper_trades", "fund_paper_orders", "fund_paper_equity",
            "fund_paper_session_funds", "fund_paper_sessions",
        ) if f"DELETE FROM dbo.{table}" in sql) for sql in statements[1:]]
        self.assertEqual(delete_tables, [
            "fund_paper_trades", "fund_paper_orders", "fund_paper_equity",
            "fund_paper_session_funds", "fund_paper_sessions",
        ])
        self.assertTrue(all(values == ("session-id",) for _, values in conn.db_cursor.calls[1:]))
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)

    def test_delete_running_session_does_not_delete_any_rows(self):
        conn = Connection()
        conn.db_cursor.rows = [("running",)]
        repo = FundRepository(settings(), connection_factory=lambda: conn)
        delete_session = getattr(repo, "delete_session", None)

        self.assertTrue(callable(delete_session), "基金仓储需要提供已停止任务删除操作")
        if not callable(delete_session):
            return
        deleted = delete_session("session-id")

        self.assertFalse(deleted)
        self.assertEqual(len(conn.db_cursor.calls), 1)
        self.assertTrue(conn.committed)


if __name__ == "__main__":
    unittest.main()
