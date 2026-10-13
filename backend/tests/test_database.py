import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings
from app.database import _prepare_odbc_package_path, create_connection


class DatabaseConnectionTests(unittest.TestCase):
    def settings(self, authentication: str) -> object:
        text = f"""[database]
server=localhost
database=QuantBacktest
authentication={authentication}
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
"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.ini"
            path.write_text(text, encoding="utf-8")
            return load_settings(path).database

    def test_uses_windows_integrated_auth_without_password(self) -> None:
        calls: list[dict[str, object]] = []
        connection = types.SimpleNamespace(timeout=0)

        def fake_connect(**kwargs: object) -> object:
            calls.append(kwargs)
            return connection

        result = create_connection(self.settings("windows"), connector=fake_connect)

        self.assertIs(result, connection)
        self.assertEqual(calls[0]["trusted_connection"], "yes")
        self.assertNotIn("pwd", calls[0])
        self.assertEqual(connection.timeout, 25)

    def test_uses_sql_auth_only_when_selected(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_connect(**kwargs: object) -> object:
            calls.append(kwargs)
            return types.SimpleNamespace(timeout=0)

        create_connection(self.settings("sql"), connector=fake_connect)

        self.assertEqual(calls[0]["uid"], "quant_user")
        self.assertEqual(calls[0]["pwd"], "local-secret")

    def test_stages_odbc_package_when_source_path_contains_non_ascii(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "量化模拟" / "mssql_python_odbc"
            driver = source / "libs" / "windows" / "x64"
            driver.mkdir(parents=True)
            (source / "__init__.py").write_text("", encoding="utf-8")
            (driver / "msodbcsql18.dll").write_bytes(b"driver")
            (driver / "mssql-auth.dll").write_bytes(b"auth")
            module = types.ModuleType("mssql_python_odbc")
            module.__file__ = str(source / "__init__.py")

            with patch.dict(sys.modules, {"mssql_python_odbc": module}), \
                    patch("app.database.tempfile.gettempdir", return_value=root), \
                    patch("app.database.version", return_value="18.6.2.1"):
                _prepare_odbc_package_path()

            staged = Path(module.__file__)
            self.assertTrue(str(staged).isascii())
            self.assertTrue((staged.parent / "libs" / "windows" / "x64" / "msodbcsql18.dll").is_file())
            self.assertTrue((staged.parent / "libs" / "windows" / "x64" / "mssql-auth.dll").is_file())


if __name__ == "__main__":
    unittest.main()
