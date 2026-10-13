import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import BacktestDefaults, DatabaseSettings, FundSettings, MarketDataSettings, PaperTradingSettings, RedisSettings, Settings
from app.fund_data.models import FundBar, FundDistribution, FundNavPoint
from app.fund_paper.manager import FundPaperTradingManager
from app.fund_paper.strategy import (
    execute_exchange_trade, execute_open_nav_trade, get_fund_strategy_catalog,
    portfolio_target_weights, strategy_signal,
)

CHINA = timezone(timedelta(hours=8))


def app_settings():
    return Settings(
        DatabaseSettings("localhost", "QuantBacktest", "windows", "", "", True, True, 5, 20),
        RedisSettings("127.0.0.1", 6379, 0, "", 2, 300, 60, 60),
        MarketDataSettings(2, 0), BacktestDefaults(100000, 5, 20, 0.0003, 5, 0.001, 0.0005),
        paper_trading=PaperTradingSettings(), fund=FundSettings(worker_interval_seconds=5),
    )


class FakeRepository:
    def __init__(self):
        self.sessions = {}
        self.cancelled_on_start = 0

    def upsert_instruments(self, items): pass
    def create_session(self, session_id, payload):
        session = dict(payload, session_id=session_id, status="running", cash=payload["initial_capital"],
                       market_value=0, equity=payload["initial_capital"], total_return=0, max_drawdown=0,
                       peak_equity=payload["initial_capital"], last_rebalanced_at=None, error_message=None,
                       orders=[], trades=[], equity_curve=[])
        session["funds"] = [dict(
            symbol=item["symbol"], name=item["name"], fund_type=payload["fund_type"],
            shares=0.0, entry_date=None, entry_price=None, cost_basis=0.0,
            last_price=None, last_quote_at=None,
        ) for item in payload["funds"]]
        self.sessions[session_id] = session

    def get_running_sessions(self):
        return [item for item in self.sessions.values() if item["status"] == "running"]
    def list_sessions(self): return list(self.sessions.values())
    def get_session(self, session_id): return self.sessions.get(session_id)
    def set_session_message(self, session_id, message): self.sessions[session_id]["error_message"] = message
    def stop_session(self, session_id, stopped_at):
        item = self.sessions.get(session_id)
        if not item or item["status"] != "running": return False
        item["status"] = "stopped"
        item["completed_at"] = stopped_at
        for order in item["orders"]:
            if order["status"] == "pending": order["status"] = "cancelled"
        return True
    def cancel_stale_orders_on_start(self):
        count = 0
        for session in self.sessions.values():
            for order in session["orders"]:
                if order["order_mode"] == "nav_next" and order["status"] == "pending":
                    order["status"] = "cancelled"
                    count += 1
        self.cancelled_on_start += count
        return count
    def apply_fund_update(self, session_id, processed_at, account, fund_updates, orders, trades, equity_point):
        session = self.sessions[session_id]
        if session["status"] != "running": return False
        session.update(account)
        session["last_processed_at"] = processed_at
        for update in fund_updates:
            current = next(item for item in session["funds"] if item["symbol"] == update["symbol"])
            current.update(update)
        for order in orders:
            current = next((item for item in session["orders"] if item["idempotency_key"] == order["idempotency_key"]), None)
            if current: current.update(order)
            else: session["orders"].append(dict(order))
        for trade in trades:
            if not any(item["idempotency_key"] == trade["idempotency_key"] for item in session["trades"]):
                session["trades"].append(dict(trade))
        session["equity_curve"].append(dict(equity_point))
        return True


class FakeMarket:
    def __init__(self):
        self.funds = {
            ("open_end", "000001"): {"fund_type": "open_end", "symbol": "000001", "name": "华夏成长",
                                      "category": "混合型", "subscription_status": "开放申购",
                                      "redemption_status": "开放赎回", "fee_description": "0.15%"},
            ("etf", "510300"): {"fund_type": "etf", "symbol": "510300", "name": "沪深300ETF",
                                "category": "", "subscription_status": "未知", "redemption_status": "未知",
                                "fee_description": ""},
        }
        self.nav = {"000001": [
            FundNavPoint(date(2026, 10, 9), 1.0, 1.0, 0),
            FundNavPoint(date(2026, 10, 12), 1.0, 1.0, 0),
            FundNavPoint(date(2026, 10, 13), 1.1, 1.1, 0.1),
        ]}
        self.quotes = {}
        self.distributions = {}

    def search(self, query, fund_type=None, limit=100):
        return [item for (kind, symbol), item in self.funds.items()
                if (fund_type is None or kind == fund_type) and (not query or query in symbol or query in item["name"])][:limit]
    def get_nav(self, fund_type, symbol, start_date=None, end_date=None): return self.nav.get(symbol, [])
    def get_bars(self, fund_type, symbol, start_date, end_date): return []
    def get_quote(self, fund_type, symbol, force_refresh=False):
        return self.quotes.get((fund_type, symbol), {
            "subscription_status": "开放申购", "redemption_status": "开放赎回", "stale": False,
        })
    def get_distributions(self, fund_type, symbol): return self.distributions.get(symbol, [])


class FundPaperTests(unittest.TestCase):
    def test_fund_catalog_contains_independent_portfolio_strategies(self):
        # 基金策略目录应与股票策略目录独立，并保存低波动参数定义。
        strategies = get_fund_strategy_catalog()

        self.assertEqual(
            [item["code"] for item in strategies],
            ["fund_equal_rebalance", "fund_low_volatility", "fund_relative_strength"],
        )
        self.assertIn("risk_lookback_days", [item["key"] for item in strategies[1]["parameters"]])
        visible = FundPaperTradingManager.get_strategies()
        self.assertEqual([item["code"] for item in visible[:3]], [item["code"] for item in strategies])
        self.assertFalse(next(item for item in visible if item["code"] == "dual_ma")["available_for_new_tasks"])

    def test_equal_rebalance_assigns_equal_target_weights(self):
        # 初始组合没有信号过滤时，目标权重按基金数量等分。
        bars = {symbol: [FundBar(date(2026, 10, 12), 1, 1, 1, 1)] for symbol in ("510300", "510500")}

        weights = portfolio_target_weights("fund_equal_rebalance", bars, {})

        self.assertEqual(weights, {"510300": 0.5, "510500": 0.5})

    def test_low_volatility_weights_prefer_stable_funds_and_fallback_to_equal(self):
        # 风险配置偏向低波动序列；历史窗口不足时稳定退回等权。
        stable = [10.0, 10.1, 10.0, 10.1, 10.0]
        volatile = [10.0, 9.0, 11.0, 9.0, 11.0]
        bars = {
            "stable": [FundBar(date(2026, 10, 1) + timedelta(days=i), value, value, value, value) for i, value in enumerate(stable)],
            "volatile": [FundBar(date(2026, 10, 1) + timedelta(days=i), value, value, value, value) for i, value in enumerate(volatile)],
        }

        weights = portfolio_target_weights("fund_low_volatility", bars, {"risk_lookback_days": 4})
        fallback = portfolio_target_weights("fund_low_volatility", {"stable": bars["stable"][:2], "volatile": bars["volatile"][:2]},
                                            {"risk_lookback_days": 4})

        self.assertGreater(weights["stable"], weights["volatile"])
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertEqual(fallback, {"stable": 0.5, "volatile": 0.5})

    def test_relative_strength_requires_positive_momentum_and_trend_filter(self):
        # 负动量基金即使在候选池中也不能入选。
        series = {
            "winner": [10.0, 10.0, 11.0, 12.0, 13.0],
            "weak": [10.0, 10.0, 9.0, 8.0, 7.0],
        }
        bars = {
            symbol: [FundBar(date(2026, 10, 1) + timedelta(days=i), value, value, value, value)
                     for i, value in enumerate(values)]
            for symbol, values in series.items()
        }

        weights = portfolio_target_weights("fund_relative_strength", bars, {
            "momentum_period_days": 2, "trend_ma_period": 3, "max_positions": 1,
        })

        self.assertEqual(weights, {"winner": 1.0})

    def test_low_volatility_parameter_is_stored_with_the_experiment(self):
        repo, market = FakeRepository(), FakeMarket()
        manager = FundPaperTradingManager(repo, market, app_settings())

        session_id = manager.create_experiment("低波动基金组合", "open_end", ["000001"], "fund_low_volatility", 1000,
                                               {"risk_lookback_days": 30, "rebalance_interval_days": 5})

        self.assertEqual(repo.get_session(session_id)["risk_lookback_days"], 30)
        self.assertEqual(repo.get_session(session_id)["rebalance_interval_days"], 5)

    def test_open_end_relative_strength_uses_accumulated_nav_for_signals(self):
        # 单位净值横盘时累计净值仍上涨，轮动信号应按累计净值生成。
        repo, market = FakeRepository(), FakeMarket()
        market.nav["000001"] = [
            FundNavPoint(date(2026, 10, 9), 1.0, 1.0, 0),
            FundNavPoint(date(2026, 10, 12), 1.0, 1.2, 0),
            FundNavPoint(date(2026, 10, 13), 1.0, 1.3, 0),
        ]
        manager = FundPaperTradingManager(repo, market, app_settings(), clock=lambda: datetime(2026, 10, 13, 18, tzinfo=CHINA))
        session_id = manager.create_experiment("累计净值轮动", "open_end", ["000001"], "fund_relative_strength", 1000,
            {"momentum_period_days": 2, "trend_ma_period": 3, "max_positions": 1})

        manager.run_cycle()

        task = repo.get_session(session_id)
        self.assertEqual([(order["side"], order["status"]) for order in task["orders"]], [("buy", "pending")])
        self.assertEqual(task["orders"][0]["amount"], 1000)

    def test_open_end_weighted_rebalance_queues_target_cash_by_weight(self):
        # 开放式基金每只待申购金额按账户目标权重计算。
        manager = FundPaperTradingManager(FakeRepository(), FakeMarket(), app_settings())
        states = {
            symbol: {"symbol": symbol, "shares": 0, "cost_basis": 0}
            for symbol in ("000001", "000002")
        }
        market_data = {
            symbol: {"price": 10, "date": date(2026, 10, 13), "stale": False,
                     "subscription_status": "开放申购", "redemption_status": "开放赎回"}
            for symbol in states
        }
        orders = []
        ready = manager._queue_open_weighted_rebalance(
            {"session_id": "weighted", "strategy_code": "fund_equal_rebalance", "redemption_fee_rate": 0.005},
            states, market_data, {"000001": 0.5, "000002": 0.5}, 1000, [], orders,
        )

        self.assertTrue(ready)
        self.assertEqual([(order["symbol"], order["side"], order["amount"]) for order in orders], [
            ("000001", "buy", 500), ("000002", "buy", 500),
        ])

    def test_exchange_weighted_rebalance_sells_overweight_then_buys_underweight(self):
        # 场内组合先卖出超配份额，再把回笼现金用于低配标的。
        manager = FundPaperTradingManager(FakeRepository(), FakeMarket(), app_settings())
        states = {
            "510300": {"symbol": "510300", "shares": 1000, "cost_basis": 10000},
            "510500": {"symbol": "510500", "shares": 0, "cost_basis": 0},
        }
        market_data = {
            symbol: {"price": 10, "stale": False}
            for symbol in states
        }
        session = {"session_id": "exchange-weighted", "fund_type": "etf", "strategy_code": "fund_equal_rebalance",
                   "commission_rate": 0, "minimum_commission": 0, "stamp_duty_rate": 0, "slippage_rate": 0}
        orders, trades = [], []

        cash = manager._rebalance_exchange_weights(
            session, states, market_data, {"510300": 0.5, "510500": 0.5}, 0, orders, trades,
            datetime(2026, 10, 13, 10, tzinfo=CHINA),
        )

        self.assertEqual([(trade["symbol"], trade["side"], trade["quantity"]) for trade in trades], [
            ("510300", "sell", 500), ("510500", "buy", 500),
        ])
        self.assertEqual(cash, 0)

    def test_exchange_buy_rounds_down_to_whole_lots_and_reserves_commission(self):
        cash, shares, trade = execute_exchange_trade(
            "buy", 2, cash=1000, budget=600, shares=0,
            commission_rate=0.0003, minimum_commission=5, stamp_duty_rate=0,
            slippage_rate=0,
        )

        self.assertEqual(trade["quantity"], 200)
        self.assertEqual(trade["commission"], 5)
        self.assertEqual(shares, 200)
        self.assertEqual(cash, 595)

    def test_exchange_buy_does_not_spend_more_than_available_cash(self):
        cash, shares, trade = execute_exchange_trade(
            "buy", 2, cash=100, budget=1000, shares=0,
            commission_rate=0.0003, minimum_commission=5, stamp_duty_rate=0,
            slippage_rate=0,
        )

        self.assertIsNone(trade)
        self.assertEqual(cash, 100)
        self.assertEqual(shares, 0)

    def test_open_nav_purchase_applies_fee_before_calculating_fractional_units(self):
        cash, shares, trade = execute_open_nav_trade("buy", 1.2, cash=1200, shares=0, amount=1200, fee_rate=0.001)

        self.assertEqual(shares, 999)
        self.assertEqual(trade["fee"], 1.2)
        self.assertEqual(cash, 0)

    def test_shared_dual_ma_strategy_generates_a_fund_buy_signal(self):
        bars = [FundBar(date(2026, 10, 9), 1, 1, 1, 1), FundBar(date(2026, 10, 12), 1, 1, 1, 1)]

        signal = strategy_signal("dual_ma", bars, 1.1, {"short_ma_period": 1, "long_ma_period": 2})

        self.assertEqual(signal["side"], "buy")

    def test_open_fund_signal_waits_for_the_next_distinct_nav_to_fill(self):
        repo, market = FakeRepository(), FakeMarket()
        manager = FundPaperTradingManager(repo, market, app_settings(), clock=lambda: datetime(2026, 10, 13, 18, tzinfo=CHINA))
        session_id = manager.create_experiment("基金双均线", "open_end", ["000001"], "dual_ma", 1000,
                                               {"short_ma_period": 1, "long_ma_period": 2})

        manager.run_cycle()
        task = repo.get_session(session_id)
        self.assertEqual([order["status"] for order in task["orders"]], ["pending"])
        self.assertEqual(task["trades"], [])

        manager.run_cycle()
        self.assertEqual(len(task["orders"]), 1)
        market.nav["000001"].append(FundNavPoint(date(2026, 10, 14), 1.2, 1.2, 0.09))
        manager.run_cycle()

        self.assertEqual(task["orders"][0]["status"], "filled")
        self.assertEqual(task["orders"][0]["execution_date"], date(2026, 10, 14))
        self.assertEqual(len(task["trades"]), 1)
        self.assertGreater(task["funds"][0]["shares"], 0)

    def test_open_fund_order_is_cancelled_if_subscription_closes_before_next_nav(self):
        repo, market = FakeRepository(), FakeMarket()
        manager = FundPaperTradingManager(repo, market, app_settings(), clock=lambda: datetime(2026, 10, 14, 18, tzinfo=CHINA))
        session_id = manager.create_experiment("暂停申购边界", "open_end", ["000001"], "dual_ma", 1000,
                                               {"short_ma_period": 1, "long_ma_period": 2})

        manager.run_cycle()
        market.nav["000001"].append(FundNavPoint(date(2026, 10, 14), 1.2, 1.2, 0.09))
        market.quotes[("open_end", "000001")] = {
            "subscription_status": "暂停申购", "redemption_status": "开放赎回", "stale": False,
        }
        manager.run_cycle()

        task = repo.get_session(session_id)
        self.assertEqual(task["orders"][0]["status"], "cancelled")
        self.assertEqual(task["trades"], [])

    def test_open_fund_redemption_fills_when_redemption_is_open(self):
        repo, market = FakeRepository(), FakeMarket()
        market.nav["000001"] = [
            FundNavPoint(date(2026, 10, 9), 1.2, 1.2, 0),
            FundNavPoint(date(2026, 10, 12), 1.1, 1.1, 0),
            FundNavPoint(date(2026, 10, 13), 1.0, 1.0, 0),
        ]
        manager = FundPaperTradingManager(repo, market, app_settings(), clock=lambda: datetime(2026, 10, 13, 18, tzinfo=CHINA))
        session_id = manager.create_experiment("开放赎回", "open_end", ["000001"], "dual_ma", 1000,
                                               {"short_ma_period": 1, "long_ma_period": 2})
        task = repo.get_session(session_id)
        task["funds"][0].update(shares=1000, cost_basis=1200, entry_date=date(2026, 10, 9))

        manager.run_cycle()
        self.assertEqual(task["orders"][0]["side"], "sell")
        self.assertEqual(task["orders"][0]["status"], "pending")
        market.nav["000001"].append(FundNavPoint(date(2026, 10, 14), 0.9, 0.9, -0.1))
        manager.run_cycle()

        self.assertEqual(task["orders"][0]["status"], "filled")
        self.assertEqual(task["trades"][0]["side"], "sell")
        self.assertEqual(task["funds"][0]["shares"], 0)

    def test_open_end_experiment_can_start_while_redemption_is_paused(self):
        # 赎回暂停只限制之后卖出，不能阻止创建并等待申购信号的实验。
        repo, market = FakeRepository(), FakeMarket()
        market.funds[("open_end", "000010")] = {
            **market.funds[("open_end", "000001")], "symbol": "000010", "redemption_status": "暂停赎回",
        }
        manager = FundPaperTradingManager(repo, market, app_settings())

        session_id = manager.create_experiment("暂停赎回基金", "open_end", ["000010"], "dual_ma", 1000, {})

        self.assertEqual(repo.get_session(session_id)["status"], "running")
        self.assertEqual(repo.get_session(session_id)["funds"][0]["symbol"], "000010")

    def test_experiment_rejects_money_fund_unknown_type_and_invalid_strategy_input(self):
        repo, market = FakeRepository(), FakeMarket()
        market.funds[("open_end", "000003")] = {**market.funds[("open_end", "000001")], "symbol": "000003", "category": "货币型"}
        manager = FundPaperTradingManager(repo, market, app_settings())

        with self.assertRaises(ValueError):
            manager.create_experiment("货币基金", "open_end", ["000003"], "dual_ma", 1000, {})
        with self.assertRaises(ValueError):
            manager.create_experiment("错误类型", "stock", ["000001"], "dual_ma", 1000, {})
        with self.assertRaises(ValueError):
            manager.create_experiment("重复基金", "open_end", ["000001", "000001"], "dual_ma", 1000, {})
        with self.assertRaises(ValueError):
            manager.create_experiment("错误策略", "open_end", ["000001"], "unknown", 1000, {})
        with self.assertRaises(ValueError):
            manager.create_experiment("无效资金", "open_end", ["000001"], "dual_ma", 0, {})
        with self.assertRaises(ValueError):
            manager.create_experiment("类型不符", "open_end", ["510300"], "dual_ma", 1000, {})

        closed = {**market.funds[("open_end", "000001")], "symbol": "000009", "subscription_status": "暂停申购"}
        market.funds[("open_end", "000009")] = closed
        with self.assertRaises(ValueError):
            manager.create_experiment("暂停申购", "open_end", ["000009"], "dual_ma", 1000, {})

    def test_start_cancels_stale_open_orders_and_shutdown_joins_worker(self):
        repo, market = FakeRepository(), FakeMarket()
        manager = FundPaperTradingManager(repo, market, app_settings(), clock=lambda: datetime(2026, 10, 13, 18, tzinfo=CHINA))
        session_id = manager.create_experiment("恢复验证", "open_end", ["000001"], "dual_ma", 1000,
                                               {"short_ma_period": 1, "long_ma_period": 2})
        manager.run_cycle()

        manager.start()
        manager.shutdown()

        self.assertEqual(repo.cancelled_on_start, 1)
        self.assertEqual(repo.get_session(session_id)["orders"][0]["status"], "cancelled")
        self.assertFalse(manager._thread.is_alive())

    def test_open_fund_does_not_credit_pre_experiment_or_post_purchase_distributions(self):
        repo, market = FakeRepository(), FakeMarket()
        manager = FundPaperTradingManager(repo, market, app_settings(), clock=lambda: datetime(2026, 10, 14, 18, tzinfo=CHINA))
        session_id = manager.create_experiment("分红边界", "open_end", ["000001"], "dual_ma", 1000,
                                               {"short_ma_period": 1, "long_ma_period": 2})
        session = repo.get_session(session_id)
        session["funds"][0].update(shares=100, cost_basis=100, entry_date=date(2026, 10, 13))
        session["orders"].append({"idempotency_key": "prior-buy", "symbol": "000001", "side": "buy", "status": "filled",
                                  "execution_date": date(2026, 10, 15), "quantity": 100})
        market.nav["000001"].append(FundNavPoint(date(2026, 10, 14), 1.1, 1.1, 0))
        market.distributions["000001"] = [
            FundDistribution(date(2026, 10, 12), 0.1, "old"),
            FundDistribution(date(2026, 10, 14), 0.1, "same-day"),
        ]

        manager.run_cycle()

        self.assertEqual(session["cash"], 1000)
        self.assertEqual(session["orders"], session["orders"][:1])

    def test_stopping_one_fund_experiment_does_not_stop_another_task(self):
        repo, market = FakeRepository(), FakeMarket()
        manager = FundPaperTradingManager(repo, market, app_settings(), clock=lambda: datetime(2026, 10, 13, 18, tzinfo=CHINA))
        first = manager.create_experiment("任务一", "open_end", ["000001"], "dual_ma", 1000,
                                          {"short_ma_period": 1, "long_ma_period": 2})
        second = manager.create_experiment("任务二", "open_end", ["000001"], "dual_ma", 2000,
                                           {"short_ma_period": 1, "long_ma_period": 2})
        manager.run_cycle()

        self.assertTrue(manager.stop_session(first))
        self.assertEqual(repo.get_session(first)["status"], "stopped")
        self.assertEqual(repo.get_session(first)["orders"][0]["status"], "cancelled")
        self.assertEqual(repo.get_session(second)["status"], "running")
        self.assertEqual(repo.get_session(second)["orders"][0]["status"], "pending")

    def test_open_rotation_queues_partial_sell_and_equal_weight_buy_for_each_nav_date(self):
        manager = FundPaperTradingManager(FakeRepository(), FakeMarket(), app_settings())
        states = {
            "000001": {"symbol": "000001", "shares": 150, "cost_basis": 1200},
            "000002": {"symbol": "000002", "shares": 0, "cost_basis": 0},
        }
        market_data = {
            "000001": {"price": 10, "date": date(2026, 10, 13), "stale": False,
                        "subscription_status": "开放申购", "redemption_status": "开放赎回"},
            "000002": {"price": 10, "date": date(2026, 10, 12), "stale": False,
                        "subscription_status": "开放申购", "redemption_status": "开放赎回"},
        }
        orders = []
        ready = manager._queue_open_rotation(
            {"session_id": "rotation", "redemption_fee_rate": 0.005}, states, market_data, ["000001", "000002"],
            500, [], orders,
        )

        self.assertTrue(ready)
        self.assertEqual([(item["symbol"], item["side"]) for item in orders], [("000001", "sell"), ("000002", "buy")])
        self.assertEqual(orders[0]["quantity"], 50)
        self.assertEqual(orders[0]["signal_date"], date(2026, 10, 13))
        self.assertEqual(orders[1]["signal_date"], date(2026, 10, 12))
        self.assertAlmostEqual(orders[1]["amount"], 997.5)


if __name__ == "__main__":
    unittest.main()
