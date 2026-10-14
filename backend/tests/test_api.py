import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BacktestDefaults, DatabaseSettings, MarketDataSettings, RedisSettings, Settings
from app.api.routes.backtests import _params
from app.api.routes.paper_trading import create_paper_session
from app.api.schemas import BacktestRequest, PaperExperimentRequest
from app.main import create_app
from app.models import Bar, Quote, WatchedStock
from app.paper_trading.manager import PaperTradingManager
from app.paper_trading.strategy import get_strategy_catalog


def app_settings() -> Settings:
    return Settings(
        DatabaseSettings("localhost", "QuantBacktest", "windows", "", "private-db-secret", True, True, 8, 30),
        RedisSettings("127.0.0.1", 6379, 0, "private-redis-secret", 3, 300, 60, 180),
        MarketDataSettings(20, 2),
        BacktestDefaults(100000, 5, 20, 0.0003, 5, 0.001, 0.0005),
    )


def quote() -> Quote:
    return Quote(
        "600000", "浦发银行", 8.2, 0.1, 1.23, 8.1, 8.3, 8.0, 8.1,
        100, 820000, "AKShare 东方财富", datetime(2024, 1, 2, tzinfo=timezone.utc), True,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.watchlist: dict[str, str] = {}

    def initialize(self) -> None:
        pass

    def mark_interrupted_runs(self) -> None:
        pass

    def get_backtest(self, run_id: str):
        return {"run_id": run_id, "status": "completed"} if run_id == "run-1" else None

    def list_backtests(self, offset: int, limit: int):
        return ([{"run_id": "run-1", "symbol": "600000"}], 1)

    def list_watchlist(self):
        return [WatchedStock(symbol, name) for symbol, name in self.watchlist.items()]

    def add_watchlist(self, symbol: str, name: str) -> None:
        self.watchlist[symbol] = name

    def remove_watchlist(self, symbol: str) -> bool:
        return self.watchlist.pop(symbol, None) is not None


class FakeMarketData:
    def search_stocks(self, query: str):
        return [quote()]

    def get_quote(self, symbol: str, force_refresh: bool = False):
        return quote()

    def get_bars(self, symbol: str, start_date: date, end_date: date):
        return [Bar(date(2024, 1, 2), 10, 11, 9, 10, 8, 9, 7, 8, 100, 1000)]

    def get_intraday_bars(self, symbol: str):
        return [{
            "trade_time": datetime(2024, 1, 2, 9, 31, tzinfo=timezone.utc),
            "open": 10, "high": 11, "low": 9, "close": 10.5,
            "volume": 1000, "turnover": 10500,
        }]


class FakeBacktestManager:
    submitted_params = None

    def recover_interrupted_runs(self) -> None:
        pass

    def submit(self, params: object) -> str:
        self.submitted_params = params
        return "run-1"

    def cancel(self, run_id: str) -> bool:
        return run_id == "run-1"

    def shutdown(self) -> None:
        pass


class FakePaperTradingManager:
    experiment_session_id = "390e8400-e29b-41d4-a716-446655440000"

    def __init__(self) -> None:
        self.session: dict[str, object] | None = None
        self.sessions: list[dict[str, object]] = []
        self.strategies = get_strategy_catalog()
        self.created_parameters: dict[str, float | int] = {}
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def create_session(self, initial_capital: float | None = None) -> str:
        self.session = {"session_id": "paper-1", "status": "running", "initial_capital": initial_capital}
        return "paper-1"

    def get_current_session(self):
        return self.session

    def get_strategies(self):
        return self.strategies

    def create_experiment(
        self, task_name: str, symbols: list[str], strategy_code: str, initial_capital: float,
        parameters: dict[str, float | int] | None = None,
    ) -> str:
        self.created_parameters = parameters or {}
        session_id = self.experiment_session_id
        self.session = {
            "session_id": session_id, "task_name": task_name,
            "symbols": [{"symbol": symbol} for symbol in symbols],
            "strategy_code": strategy_code, "initial_capital": initial_capital, "status": "running",
        }
        self.sessions.append({
            **self.session, "cash": initial_capital, "equity": initial_capital,
            "total_return": 0, "max_drawdown": 0, "symbol_count": len(symbols),
        })
        return session_id

    def list_sessions(self):
        return self.sessions

    def get_session(self, session_id: str):
        if self.session and self.session["session_id"] == session_id:
            return self.session
        return None

    def stop_session(self, session_id: str):
        session = self.get_session(session_id)
        if not session or session["status"] != "running":
            return False
        session["status"] = "stopped"
        for item in self.sessions:
            if item["session_id"] == session_id:
                item["status"] = "stopped"
        return True

    def shutdown(self) -> None:
        self.closed = True


class FakeFundPaperTradingManager:
    """避免 API 单测为未被测试的基金 worker 打开真实 SQL 连接。"""

    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class PaperExperimentRouteTests(unittest.TestCase):
    """直接验证股票纸面路由的参数传递，避免 TestClient 生命周期启动阻塞。"""

    def test_defensive_route_passes_catalog_parameters_to_manager(self) -> None:
        manager = FakePaperTradingManager()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            database_ready=True, paper_trading_manager=manager,
        )))
        payload = PaperExperimentRequest.model_validate({
            "task_name": "防御策略", "symbols": ["600000"],
            "strategy_code": "defensive_mean_reversion", "initial_capital": 100000,
            "parameters": {
                "trend_ma_period": 100, "atr_multiplier": 2.5, "max_holding_days": 8,
                "cooldown_days": 4, "target_annual_volatility": 0.12,
                "max_position_ratio": 0.6,
            },
        })

        response = create_paper_session(payload, request)

        self.assertEqual(response, {"session_id": manager.experiment_session_id, "status": "running"})
        self.assertEqual(manager.session["strategy_code"], "defensive_mean_reversion")
        self.assertEqual(manager.created_parameters, payload.parameters)

    def test_defensive_route_rejects_catalog_parameter_outside_range(self) -> None:
        # 参数范围由真实 manager 的策略目录校验，避免用假管理器模拟业务验证。
        manager = PaperTradingManager(None, None, app_settings())
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            database_ready=True, paper_trading_manager=manager,
        )))
        payload = PaperExperimentRequest.model_validate({
            "task_name": "防御策略", "symbols": ["600000"],
            "strategy_code": "defensive_mean_reversion", "initial_capital": 100000,
            "parameters": {"max_position_ratio": 1.2},
        })

        with self.assertRaises(HTTPException) as error:
            create_paper_session(payload, request)

        self.assertEqual(error.exception.status_code, 422)


class BacktestParameterRouteTests(unittest.TestCase):
    """直接覆盖参数路由 helper，避免依赖 TestClient 的异步 lifespan 启动。"""

    def resolve(self, body: dict[str, object]):
        # 使用真实模型、策略目录和路由解析，仅替换请求携带的 app.state。
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            settings=app_settings(), paper_trading_manager=FakePaperTradingManager(),
        )))
        return _params(BacktestRequest.model_validate(body), request)

    def test_defensive_parameters_map_to_existing_backtest_fields(self) -> None:
        params = self.resolve({
            "symbol": "600000", "strategy_code": "defensive_mean_reversion",
            "start_date": "2024-01-01", "end_date": "2024-03-01",
            "parameters": {
                "trend_ma_period": 100, "atr_multiplier": 2.5,
                "max_holding_days": 8, "cooldown_days": 4,
                "target_annual_volatility": 0.12, "max_position_ratio": 0.6,
            },
        })

        self.assertEqual(params.position_ratio, 0.6)
        self.assertEqual(params.max_holding_days, 8)
        self.assertEqual(params.cooldown_days, 4)
        self.assertEqual(params.target_annual_volatility, 0.12)
        self.assertEqual(params.atr_period, 14)

    def test_defensive_route_rejects_each_catalog_parameter_outside_bounds(self) -> None:
        invalid = [
            {"trend_ma_period": 20}, {"atr_multiplier": 10.1},
            {"max_holding_days": 121}, {"cooldown_days": 121},
            {"target_annual_volatility": 2.1}, {"max_position_ratio": 1.1},
        ]
        for parameters in invalid:
            with self.subTest(parameters=parameters), self.assertRaises(HTTPException):
                self.resolve({
                    "symbol": "600000", "strategy_code": "defensive_mean_reversion",
                    "start_date": "2024-01-01", "end_date": "2024-03-01",
                    "parameters": parameters,
                })

    def test_defensive_route_rejects_multiple_symbols(self) -> None:
        with self.assertRaises(HTTPException):
            self.resolve({
                "symbol": "600000", "symbols": ["600000", "000333"],
                "strategy_code": "defensive_mean_reversion",
                "start_date": "2024-01-01", "end_date": "2024-03-01",
            })


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paper_manager = FakePaperTradingManager()
        self.backtest_manager = FakeBacktestManager()
        self.client_context = TestClient(
            create_app(
                app_settings(),
                repository=FakeRepository(),
                market_service=FakeMarketData(),
                backtest_manager=self.backtest_manager,
                paper_manager=self.paper_manager,
                fund_paper_manager=FakeFundPaperTradingManager(),
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def test_config_search_quote_and_adjusted_bars_use_stable_shapes(self) -> None:
        config = self.client.get("/api/app-config").json()
        search = self.client.get("/api/stocks/search?q=浦发").json()
        snapshot = self.client.get("/api/market/quotes/600000").json()
        bars = self.client.get(
            "/api/stocks/600000/bars?start_date=2024-01-01&end_date=2024-01-05&adjust=qfq"
        ).json()

        self.assertEqual(config["refresh_interval_seconds"], 60)
        self.assertEqual(config["chart_refresh_interval_seconds"], 10)
        self.assertEqual(config["intraday_refresh_interval_seconds"], 2)
        self.assertNotIn("private-db-secret", str(config))
        self.assertNotIn("private-redis-secret", str(config))
        self.assertEqual(search[0]["symbol"], "600000")
        self.assertTrue(snapshot["stale"])
        self.assertEqual(bars[0]["close"], 8)

    def test_intraday_route_returns_structured_minute_bars(self) -> None:
        response = self.client.get("/api/stocks/600000/intraday")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["close"], 10.5)
        self.assertEqual(response.json()[0]["volume"], 1000)

    def test_watchlist_routes_add_list_and_remove_normalized_symbols(self) -> None:
        added = self.client.post("/api/watchlist", json={"symbol": "SH600000", "name": "浦发银行"})

        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.json(), {"symbol": "600000", "name": "浦发银行"})
        self.assertEqual(self.client.get("/api/watchlist").json(), [added.json()])
        removed = self.client.delete("/api/watchlist/SZ600000")
        self.assertEqual(removed.status_code, 204)
        self.assertEqual(self.client.get("/api/watchlist").json(), [])

    def test_watchlist_rejects_invalid_symbol(self) -> None:
        response = self.client.post("/api/watchlist", json={"symbol": "ABC", "name": "股票"})

        self.assertEqual(response.status_code, 422)

    def test_paper_trading_routes_start_and_restore_current_session(self) -> None:
        accepted = self.client.post("/api/paper-trading/start", json={"initial_capital": 50000})

        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json()["session_id"], "paper-1")
        current = self.client.get("/api/paper-trading/current")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["status"], "running")
        self.assertTrue(self.paper_manager.started)

    def test_paper_trading_rejects_non_positive_initial_capital(self) -> None:
        response = self.client.post("/api/paper-trading/start", json={"initial_capital": 0})

        self.assertEqual(response.status_code, 422)

    def test_paper_experiment_routes_list_create_detail_and_stop_tasks(self) -> None:
        strategies = self.client.get("/api/paper-trading/strategies")
        created = self.client.post("/api/paper-trading/sessions", json={
            "task_name": "银行策略对比", "symbols": ["600000", "600028"],
            "strategy_code": "dual_ma", "initial_capital": 100000,
            "parameters": {"short_ma_period": 8, "long_ma_period": 30},
        })
        listing = self.client.get("/api/paper-trading/sessions")
        session_id = self.paper_manager.experiment_session_id
        detail = self.client.get(f"/api/paper-trading/sessions/{session_id}")
        stopped = self.client.post(f"/api/paper-trading/sessions/{session_id}/stop")

        self.assertEqual(strategies.status_code, 200)
        self.assertEqual(
            {item["code"] for item in strategies.json()},
            {"breakout_ma_atr", "defensive_mean_reversion", "dual_ma", "momentum_rotation"},
        )
        self.assertEqual(created.status_code, 202)
        self.assertEqual(created.json(), {"session_id": session_id, "status": "running"})
        self.assertEqual(self.paper_manager.created_parameters, {"short_ma_period": 8, "long_ma_period": 30})
        self.assertEqual(listing.json()[0]["task_name"], "银行策略对比")
        self.assertEqual([item["symbol"] for item in detail.json()["symbols"]], ["600000", "600028"])
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json(), {"session_id": session_id, "status": "stopped"})

    def test_paper_experiment_accepts_momentum_rotation_from_strategy_catalog(self) -> None:
        response = self.client.post("/api/paper-trading/sessions", json={
            "task_name": "强弱轮动组合实验", "symbols": ["000333", "600000", "600028", "600276", "600887", "601899"],
            "strategy_code": "momentum_rotation", "initial_capital": 100000,
            "parameters": {
                "momentum_period_days": 20, "trend_ma_period": 60,
                "max_positions": 3, "rebalance_interval_days": 7,
            },
        })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.paper_manager.session["strategy_code"], "momentum_rotation")
        self.assertEqual(self.paper_manager.created_parameters["max_positions"], 3)

    def test_paper_experiment_rejects_invalid_symbols_and_unknown_strategy(self) -> None:
        valid = {"task_name": "试验", "symbols": ["600000"], "strategy_code": "dual_ma", "initial_capital": 100000}
        invalid_requests = [
            {**valid, "task_name": "  "},
            {**valid, "symbols": ["600000", "600000"]},
            {**valid, "symbols": ["ABC"]},
            {**valid, "symbols": [f"{index:06d}" for index in range(21)]},
            {**valid, "initial_capital": 0},
            {**valid, "strategy_code": "unknown"},
        ]

        for payload in invalid_requests:
            with self.subTest(payload=payload):
                response = self.client.post("/api/paper-trading/sessions", json=payload)
                self.assertEqual(response.status_code, 422)

    def test_paper_experiment_returns_not_found_and_sql_unavailable(self) -> None:
        missing_id = "390e8400-e29b-41d4-a716-446655440001"
        missing_detail = self.client.get(f"/api/paper-trading/sessions/{missing_id}")
        missing_stop = self.client.post(f"/api/paper-trading/sessions/{missing_id}/stop")
        malformed_detail = self.client.get("/api/paper-trading/sessions/not-a-uuid")
        malformed_stop = self.client.post("/api/paper-trading/sessions/not-a-uuid/stop")
        self.client.app.state.database_ready = False
        unavailable_list = self.client.get("/api/paper-trading/sessions")

        self.assertEqual(missing_detail.status_code, 404)
        self.assertEqual(missing_stop.status_code, 404)
        self.assertEqual(malformed_detail.status_code, 422)
        self.assertEqual(malformed_stop.status_code, 422)
        self.assertEqual(unavailable_list.status_code, 503)

    def test_backtest_lifecycle_routes_validate_and_return_not_found(self) -> None:
        response = self.client.post(
            "/api/backtests",
            json={
                "symbol": "600000", "start_date": "2024-01-01", "end_date": "2024-03-01",
                "short_window": 5, "long_window": 20, "initial_capital": 100000,
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["run_id"], "run-1")
        self.assertEqual(self.client.get("/api/backtests/run-1").status_code, 200)
        self.assertEqual(self.client.get("/api/backtests/missing").status_code, 404)
        self.assertEqual(self.client.post("/api/backtests/run-1/cancel").status_code, 200)

    def test_rejects_invalid_period_relationship_and_non_a_share_code(self) -> None:
        invalid_period = self.client.post(
            "/api/backtests",
            json={
                "symbol": "600000", "start_date": "2024-01-01", "end_date": "2024-03-01",
                "short_window": 20, "long_window": 20,
            },
        )
        bad_code = self.client.get("/api/market/quotes/ABC")

        self.assertEqual(invalid_period.status_code, 422)
        self.assertEqual(bad_code.status_code, 422)

    def test_backtest_catalog_and_rotation_parameters_use_same_strategy_codes(self) -> None:
        catalog = self.client.get("/api/backtests/strategies")
        response = self.client.post("/api/backtests", json={
            "symbol": "600000", "symbols": ["600000", "000333"],
            "strategy_code": "momentum_rotation",
            "start_date": "2024-01-01", "end_date": "2024-03-01",
            "parameters": {"momentum_period_days": 15, "trend_ma_period": 45,
                           "max_positions": 2, "rebalance_interval_days": 7},
        })

        self.assertEqual({item["code"] for item in catalog.json()},
                         {"breakout_ma_atr", "defensive_mean_reversion", "dual_ma", "momentum_rotation"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.backtest_manager.submitted_params.symbols, ("600000", "000333"))
        self.assertEqual(self.backtest_manager.submitted_params.trend_ma_period, 45)

    def test_defensive_backtest_maps_parameters_and_rejects_multiple_symbols(self) -> None:
        catalog = self.client.get("/api/backtests/strategies").json()
        accepted = self.client.post("/api/backtests", json={
            "symbol": "600000", "strategy_code": "defensive_mean_reversion",
            "start_date": "2024-01-01", "end_date": "2024-03-01",
            "parameters": {
                "trend_ma_period": 100, "atr_multiplier": 2.5,
                "max_holding_days": 8, "cooldown_days": 4,
                "target_annual_volatility": 0.12, "max_position_ratio": 0.6,
            },
        })
        multiple = self.client.post("/api/backtests", json={
            "symbol": "600000", "symbols": ["600000", "000333"],
            "strategy_code": "defensive_mean_reversion",
            "start_date": "2024-01-01", "end_date": "2024-03-01",
        })

        self.assertIn("defensive_mean_reversion", {item["code"] for item in catalog})
        self.assertEqual(accepted.status_code, 202)
        params = self.backtest_manager.submitted_params
        self.assertEqual(params.position_ratio, 0.6)
        self.assertEqual(params.max_holding_days, 8)
        self.assertEqual(params.cooldown_days, 4)
        self.assertEqual(params.target_annual_volatility, 0.12)
        self.assertEqual(params.atr_period, 14)
        self.assertEqual(multiple.status_code, 422)

    def test_defensive_backtest_rejects_out_of_range_strategy_parameters(self) -> None:
        response = self.client.post("/api/backtests", json={
            "symbol": "600000", "strategy_code": "defensive_mean_reversion",
            "start_date": "2024-01-01", "end_date": "2024-03-01",
            "parameters": {"cooldown_days": 121},
        })

        self.assertEqual(response.status_code, 422)

    def test_backtest_rejects_unknown_parameters_and_duplicate_symbols(self) -> None:
        basic = {"symbol": "600000", "strategy_code": "momentum_rotation",
                 "start_date": "2024-01-01", "end_date": "2024-03-01"}
        invalid = [
            {**basic, "parameters": {"unknown": 2}},
            {**basic, "symbols": ["600000", "600000"]},
            {**basic, "parameters": {"max_positions": 1.5}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/backtests", json=payload).status_code, 422)

    def test_legacy_single_stock_ma_windows_remain_accepted(self) -> None:
        response = self.client.post("/api/backtests", json={
            "symbol": "600000", "start_date": "2024-01-01", "end_date": "2024-03-01",
            "short_window": 125, "long_window": 251,
        })

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.backtest_manager.submitted_params.long_window, 251)


if __name__ == "__main__":
    unittest.main()
