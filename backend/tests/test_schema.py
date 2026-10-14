import unittest
from pathlib import Path


class SqlSchemaTests(unittest.TestCase):
    def test_defensive_columns_exist_in_creation_and_idempotent_migrations(self) -> None:
        schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(encoding="utf-8")
        # 每个独立列必须同时支持新库建表与旧库幂等升级，禁止借用 JSON 保存策略状态。
        columns = {
            "backtest_runs": ("max_holding_days", "cooldown_days", "target_annual_volatility"),
            "paper_sessions": ("max_holding_days", "cooldown_days", "target_annual_volatility", "max_position_ratio"),
            "paper_session_symbols": ("last_exit_date", "last_decision_date"),
            "paper_trades": ("signal_date",),
        }
        for table, names in columns.items():
            creation = schema.split(f"CREATE TABLE dbo.{table} (", 1)[1].split("\n    );", 1)[0]
            for name in names:
                with self.subTest(table=table, column=name):
                    self.assertIn(name, creation)
                    self.assertIn(f"COL_LENGTH(N'dbo.{table}', N'{name}') IS NULL", schema)
        self.assertNotRegex(schema, r"(?i)\bJSON\s*(?:,|\)|NULL|NOT NULL)")

    def test_schema_has_normalized_tables_and_no_json_column_type(self) -> None:
        schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(encoding="utf-8")

        for table in (
            "stocks", "daily_bars", "daily_bar_coverage", "corporate_actions", "market_watchlist",
            "backtest_runs", "backtest_run_symbols", "backtest_equity", "backtest_trades", "backtest_warnings",
            "paper_sessions", "paper_session_symbols", "paper_trades", "paper_equity",
        ):
            self.assertIn(table, schema)
        self.assertIn("ex_date DATE NULL", schema)
        self.assertNotRegex(schema, r"(?i)\bJSON\s*(?:,|\)|NULL|NOT NULL)")

    def test_backtest_strategy_migration_only_adds_structured_columns(self) -> None:
        schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(encoding="utf-8")

        for name in ("ma_period", "breakout_period", "atr_period", "atr_multiplier",
                     "momentum_period_days", "trend_ma_period", "max_positions", "rebalance_interval_days"):
            self.assertIn(f"COL_LENGTH(N'dbo.backtest_runs', N'{name}')", schema)
        self.assertIn("COL_LENGTH(N'dbo.backtest_trades', N'symbol')", schema)
        self.assertIn("CREATE TABLE dbo.backtest_run_symbols", schema)

    def test_paper_schema_supports_multiple_named_tasks_without_losing_legacy_rows(self) -> None:
        schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(encoding="utf-8")

        self.assertIn("task_name NVARCHAR(100) NOT NULL", schema)
        self.assertIn("short_ma_period INT NOT NULL", schema)
        self.assertIn("long_ma_period INT NOT NULL", schema)
        self.assertIn("momentum_period_days INT NOT NULL", schema)
        self.assertIn("trend_ma_period INT NOT NULL", schema)
        self.assertIn("max_positions INT NOT NULL", schema)
        self.assertIn("rebalance_interval_days INT NOT NULL", schema)
        self.assertIn("last_rebalanced_at DATETIME2 NULL", schema)
        self.assertIn("COL_LENGTH(N'dbo.paper_sessions', N'momentum_period_days')", schema)
        self.assertIn("COL_LENGTH(N'dbo.paper_sessions', N'last_rebalanced_at')", schema)
        self.assertIn("ends_at DATETIME2 NULL", schema)
        self.assertIn("'stopped'", schema)
        self.assertIn("DROP INDEX UX_paper_sessions_running", schema)
        self.assertIn("CREATE INDEX IX_paper_sessions_status_started", schema)
        self.assertNotRegex(schema, r"(?i)CREATE\s+UNIQUE\s+INDEX\s+UX_paper_sessions_running")

    def test_fund_tables_are_separate_structured_idempotent_ledger_tables(self) -> None:
        schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(encoding="utf-8")

        tables = (
            "fund_instruments", "fund_daily_bars", "fund_nav_history", "fund_distributions",
            "fund_paper_sessions", "fund_paper_session_funds", "fund_paper_orders",
            "fund_paper_trades", "fund_paper_equity",
        )
        for table in tables:
            self.assertIn(f"IF OBJECT_ID(N'dbo.{table}', N'U') IS NULL", schema)
            self.assertIn(f"CREATE TABLE dbo.{table}", schema)
        self.assertIn("PRIMARY KEY (fund_type, symbol)", schema)
        self.assertIn("idempotency_key NVARCHAR(180) NOT NULL UNIQUE", schema)
        self.assertIn("last_rebalanced_at DATETIME2 NULL", schema)
        self.assertIn("risk_lookback_days INT NOT NULL", schema)
        self.assertIn("COL_LENGTH(N'dbo.fund_paper_sessions', N'risk_lookback_days')", schema)
        self.assertIn("entry_atr DECIMAL(19,8) NULL", schema)
        self.assertIn("highest_price DECIMAL(19,8) NULL", schema)
        self.assertNotRegex(schema, r"(?i)\bJSON\s*(?:,|\)|NULL|NOT NULL)")


if __name__ == "__main__":
    unittest.main()
