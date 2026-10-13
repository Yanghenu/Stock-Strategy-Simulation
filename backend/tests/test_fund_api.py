import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.config import BacktestDefaults, DatabaseSettings, MarketDataSettings, RedisSettings, Settings
from app.fund_data.models import FundBar, FundInstrument, FundIntradayBar, FundNavPoint, FundQuote
from app.fund_data.service import FundMarketDataService
from app.main import create_app


def settings():
    return Settings(
        DatabaseSettings("localhost", "QuantBacktest", "windows", "", "", True, True, 5, 20),
        RedisSettings("127.0.0.1", 6379, 0, "", 2, 300, 60, 60),
        MarketDataSettings(2, 0), BacktestDefaults(100000, 5, 20, 0.0003, 5, 0.001, 0.0005),
    )


def quote(fund_type, symbol, price, nav_date=None):
    return FundQuote(symbol, symbol, fund_type, price, 0.01, price, price, price, price,
                     1000, 10000, "test", datetime(2026, 10, 13, 10), nav_date, price * 1.5,
                     "开放申购" if fund_type == "open_end" else "未知", "开放赎回", "0.15%")


class FakeRepository:
    def __init__(self):
        self.instruments = []
        self.bars = []
        self.nav = []
        self.distributions = []
        self.distribution_reads = 0

    def upsert_instruments(self, items): self.instruments.extend(items)
    def search_funds(self, query, fund_type=None, limit=100):
        rows = [item for item in self.instruments if (not query or query in item.symbol or query in item.name)]
        return [item.__dict__ for item in rows if fund_type is None or item.fund_type == fund_type][:limit]
    def upsert_bars(self, fund_type, symbol, bars): self.bars = bars
    def get_bars(self, fund_type, symbol, start_date, end_date): return self.bars
    def upsert_nav_history(self, fund_type, symbol, points): self.nav = points
    def get_nav_history(self, fund_type, symbol, start_date=None, end_date=None): return self.nav
    def upsert_distributions(self, fund_type, symbol, items): self.distributions = items
    def get_distributions(self, fund_type, symbol):
        self.distribution_reads += 1
        return self.distributions


class FakeSource:
    def __init__(self):
        self.fail = False
        self.nav_calls = 0
        self.spot_calls = 0
        self.search_calls = []
        self.fail_bars = False
        self.live_intraday = False

    def fetch_spot(self, fund_type):
        self.spot_calls += 1
        if self.fail: raise RuntimeError("upstream secret should not leak")
        return [quote(fund_type, "510300", 4.12)]
    def fetch_open_fund_snapshot(self):
        item = FundInstrument("000001", "华夏成长", "open_end", "开放申购", "开放赎回", "0.15%")
        return [item], [quote("open_end", "000001", 1.234, date(2026, 10, 12))]
    def fetch_bars(self, fund_type, symbol, start_date, end_date):
        if self.fail or self.fail_bars: raise RuntimeError("upstream secret should not leak")
        return [FundBar(date(2026, 10, 12), 4, 4.2, 3.9, 4.1, 100, 500)]
    def search_instruments(self, query, fund_type=None):
        self.search_calls.append((query, fund_type))
        if self.fail: raise RuntimeError("upstream secret should not leak")
        if query in {"510300", "沪深300ETF"} and fund_type in {None, "etf"}:
            return [FundInstrument("510300", "沪深300ETF", "etf")]
        if query in {"000001", "华夏成长"} and fund_type in {None, "open_end"}:
            return [FundInstrument("000001", "华夏成长", "open_end")]
        return []
    def fetch_intraday(self, fund_type, symbol, trade_date):
        if self.fail: raise RuntimeError("upstream secret should not leak")
        if self.live_intraday:
            return [FundIntradayBar(datetime.combine(trade_date, datetime.min.time().replace(hour=10)),
                                    4.2, 4.3, 4.1, 4.25, 100, 425)]
        return []
    def fetch_open_fund_nav(self, symbol):
        self.nav_calls += 1
        return [FundNavPoint(date(2026, 10, 12), 1.234, 2.345, 0.005)]
    def fetch_distributions(self, fund_type, symbol): return []


class FakeSqlRepository:
    def initialize(self): pass


class FakeBacktestManager:
    def recover_interrupted_runs(self): pass
    def shutdown(self): pass


class FakePaperManager:
    def start(self): pass
    def shutdown(self): pass


class FakeFundPaperManager:
    def __init__(self):
        self.session = {"session_id": "fund-session", "task_name": "ETF 趋势实验", "fund_type": "etf",
                        "strategy_code": "dual_ma", "status": "running", "started_at": datetime(2026, 10, 13),
                        "initial_capital": 100000, "cash": 100000, "market_value": 0, "equity": 100000,
                        "total_return": 0, "max_drawdown": 0, "risk_lookback_days": 60,
                        "funds": [], "orders": [], "trades": [], "equity_curve": []}
        self.started = False
        self.shutdown_called = False
        self.task_stopped = False

    def start(self): self.started = True
    def shutdown(self): self.shutdown_called = True
    def get_strategies(self): return [
        {"code": "fund_equal_rebalance", "name": "基金等权定期再平衡", "available_for_new_tasks": True, "parameters": []},
        {"code": "dual_ma", "name": "历史双均线", "available_for_new_tasks": False, "parameters": []},
    ]
    def list_sessions(self): return [self.session] if self.session else []
    def get_session(self, session_id): return self.session if self.session and session_id == self.session["session_id"] else None
    def create_experiment(self, *args): return self.session["session_id"]
    def stop_session(self, session_id):
        if not self.session or session_id != self.session["session_id"]: return False
        self.session["status"] = "stopped"
        self.task_stopped = True
        return True
    def delete_session(self, session_id):
        if not self.session or session_id != self.session["session_id"] or self.session["status"] != "stopped": return False
        self.session = None
        return True


class FundApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.source = FakeSource()
        self.service = FundMarketDataService(self.repository, self.source, settings().fund)
        self.fund_paper_manager = FakeFundPaperManager()
        app = create_app(
            settings(), repository=FakeSqlRepository(), market_service=object(),
            backtest_manager=FakeBacktestManager(), paper_manager=FakePaperManager(),
            fund_repository=self.repository, fund_market_service=self.service,
            fund_paper_manager=self.fund_paper_manager,
        )
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_search_filters_type_and_code_or_name(self):
        response = self.client.get("/api/funds/search?q=510300&fund_type=etf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["fund_type"], "etf")
        self.assertEqual(self.repository.instruments[0].symbol, "510300")
        self.assertEqual(self.source.spot_calls, 0)
        self.assertEqual(self.source.search_calls, [("510300", "etf")])

    def test_search_by_name_uses_targeted_provider_search(self):
        response = self.client.get("/api/funds/search?q=%E6%B2%AA%E6%B7%B1300ETF&fund_type=etf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["symbol"], "510300")
        self.assertEqual(self.source.search_calls, [("沪深300ETF", "etf")])
        self.assertEqual(self.source.spot_calls, 0)

    def test_open_fund_quote_marks_published_nav_date(self):
        response = self.client.get("/api/funds/open_end/000001/quote")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["latest_price"], 1.234)
        self.assertEqual(response.json()["nav_date"], "2026-10-12")
        self.assertEqual(self.source.spot_calls, 0)

    def test_exchange_quote_uses_single_fund_history_without_market_snapshot(self):
        response = self.client.get("/api/funds/etf/510300/quote?force_refresh=true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "沪深300ETF")
        self.assertEqual(response.json()["latest_price"], 4.1)
        self.assertTrue(response.json()["stale"])
        self.assertEqual(self.source.spot_calls, 0)

    def test_live_minute_quote_survives_a_daily_history_source_failure(self):
        self.source.live_intraday = True
        self.source.fail_bars = True

        response = self.client.get("/api/funds/etf/510300/quote?force_refresh=true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["latest_price"], 4.25)
        self.assertFalse(response.json()["stale"])

    def test_open_fund_intraday_route_returns_422(self):
        response = self.client.get("/api/funds/open_end/000001/intraday")
        self.assertEqual(response.status_code, 422)

    def test_upstream_failure_returns_safe_503(self):
        self.source.fail = True
        response = self.client.get("/api/funds/etf/510300/quote?force_refresh=true")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("upstream secret", response.text)

    def test_fund_paper_endpoints_list_strategies_create_and_stop_tasks(self):
        strategies = self.client.get("/api/fund-paper/strategies")
        tasks = self.client.get("/api/fund-paper/sessions")
        created = self.client.post("/api/fund-paper/sessions", json={
            "task_name": "ETF 趋势实验", "fund_type": "etf", "symbols": ["510300"],
            "strategy_code": "dual_ma", "initial_capital": 100000,
            "parameters": {"short_ma_period": 5, "long_ma_period": 20},
        })
        stopped = self.client.post("/api/fund-paper/sessions/fund-session/stop")

        self.assertEqual(strategies.status_code, 200)
        self.assertEqual(strategies.json()[0]["code"], "fund_equal_rebalance")
        self.assertTrue(strategies.json()[0]["available_for_new_tasks"])
        self.assertFalse(strategies.json()[1]["available_for_new_tasks"])
        self.assertEqual(created.json()["risk_lookback_days"], 60)
        self.assertEqual(tasks.json()[0]["task_name"], "ETF 趋势实验")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(stopped.status_code, 200)
        self.assertTrue(self.fund_paper_manager.started)
        self.assertTrue(self.fund_paper_manager.task_stopped)

    def test_fund_paper_task_not_found_returns_404(self):
        response = self.client.get("/api/fund-paper/sessions/missing")

        self.assertEqual(response.status_code, 404)

    def test_fund_paper_stopped_task_can_be_deleted(self):
        self.fund_paper_manager.session["status"] = "stopped"

        response = self.client.delete("/api/fund-paper/sessions/fund-session")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True})
        self.assertIsNone(self.fund_paper_manager.session)

    def test_fund_paper_running_task_cannot_be_deleted(self):
        response = self.client.delete("/api/fund-paper/sessions/fund-session")

        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.fund_paper_manager.session)

    def test_open_fund_history_cache_respects_configured_nav_interval(self):
        first = self.client.get("/api/funds/open_end/000001/nav")
        second = self.client.get("/api/funds/open_end/000001/nav?start_date=2026-10-12")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.source.nav_calls, 1)
        self.assertEqual(second.json()[0]["unit_nav"], 1.234)


if __name__ == "__main__":
    unittest.main()
