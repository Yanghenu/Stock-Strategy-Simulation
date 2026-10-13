import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings, public_config


class ConfigTests(unittest.TestCase):
    def write_config(self, folder: str, override: str = "") -> Path:
        path = Path(folder) / "config.ini"
        path.write_text(
            """[database]
server=localhost\\SQLEXPRESS
database=QuantBacktest
authentication=sql
username=quant_user
password=local-secret
encrypt=yes
trust_server_certificate=yes
connection_timeout_seconds=8
statement_timeout_seconds=25

[redis]
host=127.0.0.1
port=6379
db=0
password=redis-secret
socket_timeout_seconds=3
cache_ttl_seconds=300
refresh_interval_seconds=60
refresh_lock_seconds=180

[market_data]
request_timeout_seconds=20
max_retries=2

[backtest]
default_initial_capital=100000
default_short_window=5
default_long_window=20
default_commission_rate=0.0003
default_minimum_commission=5
default_stamp_duty_rate=0.001
default_slippage_rate=0.0005
""" + override,
            encoding="utf-8",
        )
        return path

    def test_loads_local_connection_settings_and_market_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(self.write_config(folder))

        self.assertEqual(settings.database.server, "localhost\\SQLEXPRESS")
        self.assertEqual(settings.redis.port, 6379)
        self.assertEqual(settings.redis.refresh_interval_seconds, 60)
        self.assertEqual(settings.market_data.chart_refresh_interval_seconds, 10)
        self.assertEqual(settings.market_data.intraday_refresh_interval_seconds, 2)
        self.assertEqual(settings.backtest.long_window, 20)

    def test_loads_configured_chart_refresh_interval_without_changing_paper_interval(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder)
            text = path.read_text(encoding="utf-8").replace(
                "max_retries=2", "max_retries=2\nchart_refresh_interval_seconds=5"
            )
            path.write_text(text, encoding="utf-8")
            settings = load_settings(path)

        self.assertEqual(settings.market_data.chart_refresh_interval_seconds, 5)
        self.assertEqual(settings.redis.refresh_interval_seconds, 60)

    def test_loads_configured_intraday_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder)
            text = path.read_text(encoding="utf-8").replace(
                "max_retries=2", "max_retries=2\nintraday_refresh_interval_seconds=3"
            )
            path.write_text(text, encoding="utf-8")
            settings = load_settings(path)

        self.assertEqual(settings.market_data.intraday_refresh_interval_seconds, 3)

    def test_loads_paper_strategy_defaults_and_user_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(self.write_config(folder))

        self.assertEqual(settings.paper_trading.symbols, ("600000", "600028", "600887", "600276", "000333", "601899"))
        self.assertEqual(settings.paper_trading.ma_period, 60)
        self.assertEqual(settings.paper_trading.breakout_period, 20)
        self.assertEqual(settings.paper_trading.atr_multiplier, 2.0)

        with tempfile.TemporaryDirectory() as folder:
            config_path = self.write_config(
                folder,
                "\n[paper_trading]\nsymbols=600000,600028\nma_period=40\nbreakout_period=15\natr_period=14\natr_multiplier=2.5\n",
            )
            settings = load_settings(config_path)

        self.assertEqual(settings.paper_trading.symbols, ("600000", "600028"))
        self.assertEqual(settings.paper_trading.ma_period, 40)
        self.assertEqual(settings.paper_trading.atr_period, 14)

    def test_rejects_invalid_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder).read_text(encoding="utf-8").replace(
                "refresh_interval_seconds=60", "refresh_interval_seconds=0"
            )
            config_path = Path(folder) / "config.ini"
            config_path.write_text(path, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(config_path)

    def test_rejects_chart_refresh_interval_below_safe_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder)
            text = path.read_text(encoding="utf-8").replace(
                "max_retries=2", "max_retries=2\nchart_refresh_interval_seconds=1"
            )
            path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "不能小于 5 秒"):
                load_settings(path)

    def test_rejects_intraday_refresh_interval_below_two_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder)
            text = path.read_text(encoding="utf-8").replace(
                "max_retries=2", "max_retries=2\nintraday_refresh_interval_seconds=1"
            )
            path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "不能小于 2 秒"):
                load_settings(path)

    def test_rejects_negative_retries_and_non_loopback_api_binding(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config_path = self.write_config(folder)
            text = config_path.read_text(encoding="utf-8").replace("max_retries=2", "max_retries=-1")
            config_path.write_text(text + "\n[api]\nhost=0.0.0.0\nport=8000\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_settings(config_path)

    def test_public_config_does_not_expose_database_or_redis_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(self.write_config(folder))

        response = public_config(settings)
        self.assertEqual(response["refresh_interval_seconds"], 60)
        self.assertEqual(response["chart_refresh_interval_seconds"], 10)
        self.assertEqual(response["intraday_refresh_interval_seconds"], 2)
        self.assertEqual(response["paper_trading"]["ma_period"], 60)  # type: ignore[index]
        self.assertNotIn("local-secret", repr(response))
        self.assertNotIn("redis-secret", repr(response))
        self.assertNotIn("server", response)

    def test_fund_settings_default_for_old_configs_and_load_fee_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            old_settings = load_settings(self.write_config(folder))
        self.assertEqual(old_settings.fund.exchange_refresh_interval_seconds, 10)
        self.assertEqual(old_settings.fund.default_initial_capital, 100000)

        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(
                folder,
                "\n[fund]\nexchange_refresh_interval_seconds=15\nopen_nav_refresh_interval_seconds=600\n"
                "worker_interval_seconds=20\ndefault_initial_capital=250000\ncommission_rate=0.0004\n"
                "minimum_commission=6\nstamp_duty_rate=0\nslippage_rate=0.001\n"
                "subscription_fee_rate=0.002\nredemption_fee_rate=0.003\n",
            )
            settings = load_settings(path)

        self.assertEqual(settings.fund.exchange_refresh_interval_seconds, 15)
        self.assertEqual(settings.fund.open_nav_refresh_interval_seconds, 600)
        self.assertEqual(settings.fund.worker_interval_seconds, 20)
        self.assertEqual(settings.fund.subscription_fee_rate, 0.002)

    def test_fund_settings_reject_negative_fees_and_too_fast_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder, "\n[fund]\nsubscription_fee_rate=-0.01\n")
            with self.assertRaises(ValueError):
                load_settings(path)

        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder, "\n[fund]\nexchange_refresh_interval_seconds=1\n")
            with self.assertRaises(ValueError):
                load_settings(path)

    def test_fund_public_config_exposes_intervals_and_fees_but_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(self.write_config(folder))

        result = public_config(settings)
        self.assertEqual(result["fund"]["exchange_refresh_interval_seconds"], 10)  # type: ignore[index]
        self.assertNotIn("local-secret", repr(result))
        self.assertNotIn("redis-secret", repr(result))


if __name__ == "__main__":
    unittest.main()
