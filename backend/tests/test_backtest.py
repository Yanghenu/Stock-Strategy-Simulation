import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.engine import run_backtest
from app.backtest.rotation import run_rotation_backtest
from app.models import BacktestParams, Bar, CorporateAction
from app.paper_trading.strategy import DefensiveDecision, evaluate_defensive_mean_reversion


def make_bar(day: int, close: float, adj_close: float | None = None) -> Bar:
    return Bar(
        trade_date=date(2024, 1, day),
        open=close,
        high=close,
        low=close,
        close=close,
        adj_open=adj_close if adj_close is not None else close,
        adj_high=adj_close if adj_close is not None else close,
        adj_low=adj_close if adj_close is not None else close,
        adj_close=adj_close if adj_close is not None else close,
        volume=10000,
        turnover=0,
    )


def params(**overrides: object) -> BacktestParams:
    values: dict[str, object] = {
        "symbol": "600000",
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 1, 31),
        "short_window": 2,
        "long_window": 3,
        "initial_capital": 100000,
        "position_ratio": 1,
        "commission_rate": 0,
        "minimum_commission": 0,
        "stamp_duty_rate": 0,
        "slippage_rate": 0,
    }
    values.update(overrides)
    return BacktestParams(**values)  # type: ignore[arg-type]


class BacktestEngineTests(unittest.TestCase):
    @staticmethod
    def trading_dates(count: int) -> list[date]:
        current = date(2024, 1, 1)
        result = []
        while len(result) < count:
            if current.weekday() < 5:
                result.append(current)
            current += timedelta(days=1)
        return result

    @staticmethod
    def dated_bars(closes: list[float]) -> list[Bar]:
        result = []
        for trade_date, close in zip(BacktestEngineTests.trading_dates(len(closes)), closes):
            result.append(Bar(
                trade_date, close, close, close, close,
                close, close, close, close, 10000, 0,
            ))
        return result

    def test_defensive_strategy_stays_in_cash_during_downtrend(self) -> None:
        bars = self.dated_bars([200 - day for day in range(80)])
        result = run_backtest(bars, [], params(
            strategy_code="defensive_mean_reversion", trend_ma_period=21, atr_period=14,
        ))

        self.assertEqual(result.trades, [])
        self.assertTrue(all(point.shares == 0 for point in result.equity_curve))

    def test_defensive_strategy_benchmark_uses_full_initial_capital(self) -> None:
        bars = self.dated_bars([10 + day / 10 for day in range(4)])
        bars[0] = Bar(bars[0].trade_date, 10, 10, 10, 10, 10, 10, 10, 10, 10000, 0)
        result = run_backtest(bars, [], params(
            strategy_code="defensive_mean_reversion", trend_ma_period=21, position_ratio=0.25,
            commission_rate=0.001, minimum_commission=5, stamp_duty_rate=0.001,
            slippage_rate=0.01,
        ))

        self.assertAlmostEqual(result.equity_curve[0].benchmark_equity, 98921.02)

    def test_defensive_close_signal_fills_next_open_with_dynamic_whole_lot_size(self) -> None:
        closes = [100 + index * 0.5 for index in range(159)]
        closes.extend([closes[-1] + 0.5 - 10, closes[-1] + 1 - 25, closes[-1] + 1 - 25])
        bars = [Bar(
            trade_date, close, close + 0.2, close - 0.2, close,
            close, close + 0.2, close - 0.2, close, 10000, 0,
        ) for trade_date, close in zip(self.trading_dates(len(closes)), closes)]
        bars[-1] = Bar(
            bars[-1].trade_date, bars[-1].close + 1, bars[-1].close + 1,
            bars[-1].close, bars[-1].close, bars[-1].close + 1,
            bars[-1].close + 1, bars[-1].close, bars[-1].close, 10000, 0,
        )
        signal = evaluate_defensive_mean_reversion(
            bars[:-1], None, None, None, None, 120, 14, 2, 5, 3, 0.15, 0.7,
        )
        result = run_backtest(bars, [], params(
            strategy_code="defensive_mean_reversion", position_ratio=0.7,
            trend_ma_period=120, atr_period=14, end_date=bars[-1].trade_date,
        ))

        self.assertEqual(signal.action, "buy")
        buy = next(trade for trade in result.trades if trade.side == "buy")
        self.assertEqual(buy.signal_date, bars[-2].trade_date)
        self.assertEqual(buy.trade_date, bars[-1].trade_date)
        self.assertEqual(buy.price, bars[-1].open)
        self.assertEqual(buy.quantity, int((100000 * signal.position_ratio) // (buy.price * 100)) * 100)

    def test_defensive_cooldown_starts_on_actual_sell_execution_date(self) -> None:
        bars = self.dated_bars([10] * 5)
        observed_exit_dates: list[date | None] = []

        def decisions(signal_bars, entry_date, entry_price, entry_atr, last_exit_date, *args):
            observed_exit_dates.append(last_exit_date)
            if entry_date is None and last_exit_date is None and signal_bars[-1].trade_date == bars[0].trade_date:
                return DefensiveDecision("buy", "test entry", 1, 0.5)
            if entry_date is not None:
                return DefensiveDecision("sell", "test exit")
            return DefensiveDecision(None, "cooldown")

        with patch("app.backtest.engine.evaluate_defensive_mean_reversion", side_effect=decisions):
            result = run_backtest(bars, [], params(
                strategy_code="defensive_mean_reversion", trend_ma_period=21,
            ))

        self.assertEqual([(trade.side, trade.trade_date) for trade in result.trades], [
            ("buy", bars[1].trade_date), ("sell", bars[2].trade_date),
        ])
        self.assertEqual(observed_exit_dates[2], bars[2].trade_date)
        self.assertEqual(observed_exit_dates[3:], [bars[2].trade_date, bars[2].trade_date])

    def test_breakout_uses_prior_closed_bars_and_sells_after_atr_stop(self) -> None:
        bars = [make_bar(day, close) for day, close in enumerate([10, 10, 10, 12, 12, 10, 8], 1)]
        for index in range(3):
            bars[index] = Bar(**{**bars[index].__dict__, "high": 10.5, "low": 9.5})
        bars[4] = Bar(**{**bars[4].__dict__, "high": 12.5})

        result = run_backtest(bars, [], params(
            strategy_code="breakout_ma_atr", ma_period=2,
            breakout_period=2, atr_period=2, atr_multiplier=2,
        ))

        self.assertEqual([(item.side, item.trade_date) for item in result.trades], [
            ("buy", date(2024, 1, 5)), ("sell", date(2024, 1, 7)),
        ])
        self.assertEqual(result.trades[0].signal_date, date(2024, 1, 4))

    def test_rotation_sells_weak_stock_before_buying_stronger_stock(self) -> None:
        bars = {
            "600000": [make_bar(day, close) for day, close in enumerate([10, 12, 13, 11, 10, 9], 1)],
            "000333": [make_bar(day, close) for day, close in enumerate([10, 10, 10, 13, 14, 15], 1)],
        }
        result = run_rotation_backtest(bars, {}, params(
            strategy_code="momentum_rotation", symbols=("600000", "000333"),
            momentum_period_days=1, trend_ma_period=2, max_positions=1,
            rebalance_interval_days=1,
        ))

        self.assertEqual([(item.side, item.symbol, item.trade_date) for item in result.trades], [
            ("buy", "600000", date(2024, 1, 3)),
            ("sell", "600000", date(2024, 1, 5)),
            ("buy", "000333", date(2024, 1, 5)),
        ])
        self.assertTrue(all(item.quantity % 100 == 0 for item in result.trades))
        self.assertGreaterEqual(min(point.cash for point in result.equity_curve), 0)

    def test_rotation_cancellation_returns_no_partial_result(self) -> None:
        bars = {"600000": [make_bar(day, 10) for day in range(1, 4)]}
        with self.assertRaises(InterruptedError):
            run_rotation_backtest(
                bars, {}, params(strategy_code="momentum_rotation", symbols=("600000",)),
                cancel_check=lambda: True,
            )

    def test_executes_close_signal_at_next_trading_day_open(self) -> None:
        bars = [
            make_bar(1, 10), make_bar(2, 9), make_bar(3, 8),
            make_bar(4, 12), make_bar(5, 20), make_bar(6, 22),
        ]
        bars[4] = Bar(**{**bars[4].__dict__, "open": 15, "low": 15})

        result = run_backtest(bars, [], params())

        buy = next(trade for trade in result.trades if trade.side == "buy")
        self.assertEqual(buy.trade_date, date(2024, 1, 5))
        self.assertEqual(buy.price, 15)
        self.assertNotEqual(buy.trade_date, date(2024, 1, 4))

    def test_buys_whole_lots_and_sells_no_earlier_than_next_day(self) -> None:
        bars = [make_bar(day, close) for day, close in enumerate([10, 9, 8, 12, 10, 8, 7], 1)]

        result = run_backtest(bars, [], params())

        buys = [trade for trade in result.trades if trade.side == "buy"]
        sells = [trade for trade in result.trades if trade.side == "sell"]
        self.assertTrue(buys)
        self.assertTrue(sells)
        self.assertEqual(buys[0].quantity % 100, 0)
        self.assertGreater(sells[0].trade_date, buys[0].trade_date)

    def test_accounts_for_fees_slippage_and_cash_dividend(self) -> None:
        bars = [make_bar(day, close) for day, close in enumerate([10, 9, 8, 12, 10, 9], 1)]
        action = CorporateAction(
            symbol="600000",
            record_date=date(2024, 1, 5),
            ex_date=date(2024, 1, 6),
            cash_per_share=0.5,
            bonus_shares_per_share=0.1,
        )

        result = run_backtest(
            bars,
            [action],
            params(commission_rate=0.001, minimum_commission=5, stamp_duty_rate=0.001, slippage_rate=0.01),
        )

        self.assertTrue(result.trades)
        self.assertGreater(result.fees_total, 0)
        self.assertGreater(result.dividends_total, 0)
        self.assertEqual(result.equity_curve[-1].shares % 1, 0)

    def test_reports_unclosed_position_at_last_close_without_forced_sale(self) -> None:
        bars = [make_bar(day, close) for day, close in enumerate([10, 9, 8, 12, 13], 1)]

        result = run_backtest(bars, [], params())

        self.assertTrue(result.open_position)
        self.assertFalse(any(trade.side == "sell" for trade in result.trades))
        self.assertGreater(result.equity_curve[-1].market_value, 0)

    def test_dividend_is_credited_when_shares_are_sold_after_record_date(self) -> None:
        bars = [make_bar(day, close) for day, close in enumerate([10, 9, 8, 12, 10, 7, 6, 5], 1)]
        action = CorporateAction(
            symbol="600000",
            record_date=date(2024, 1, 5),
            ex_date=date(2024, 1, 8),
            cash_per_share=0.5,
        )

        result = run_backtest(bars, [action], params())

        self.assertEqual(result.dividends_total, 5000)
        self.assertFalse(result.open_position)

    def test_defensive_strategy_exits_bonus_shares_arriving_after_original_sale(self) -> None:
        dates = self.trading_dates(7)
        bars = [Bar(
            trade_date, 10, 10, 10, 10, 10, 10, 10, 10, 10000, 0,
        ) for trade_date in dates]
        action = CorporateAction(
            symbol="600000",
            record_date=dates[2],
            ex_date=dates[4],
            cash_per_share=0,
            bonus_shares_per_share=0.1,
        )

        def decisions(signal_bars, entry_date, entry_price, entry_atr, last_exit_date, *args):
            signal_date = signal_bars[-1].trade_date
            if entry_date is None and signal_date == dates[0]:
                return DefensiveDecision("buy", "test entry", 1, 0.5)
            if entry_date is not None and signal_date == dates[2]:
                return DefensiveDecision("sell", "test exit")
            # 送转股对应的新批次没有原持仓的入场状态，应由引擎安排确定性的退出。
            return evaluate_defensive_mean_reversion(
                signal_bars, entry_date, entry_price, entry_atr, last_exit_date, *args,
            )

        with patch("app.backtest.engine.evaluate_defensive_mean_reversion", side_effect=decisions):
            result = run_backtest(
                bars, [action], params(
                    strategy_code="defensive_mean_reversion", trend_ma_period=21,
                    start_date=dates[0], end_date=dates[-1],
                ),
            )

        self.assertEqual([(trade.side, trade.trade_date) for trade in result.trades], [
            ("buy", dates[1]), ("sell", dates[3]), ("sell", dates[5]),
        ])
        self.assertFalse(result.open_position)
        self.assertEqual(result.trades[-1].quantity, 500)

    def test_defensive_strategy_does_not_sell_ex_date_bonus_shares_on_arrival_day(self) -> None:
        dates = self.trading_dates(6)
        bars = [Bar(
            trade_date, 10, 10, 10, 10, 10, 10, 10, 10, 10000, 0,
        ) for trade_date in dates]
        action = CorporateAction(
            symbol="600000",
            record_date=dates[2],
            ex_date=dates[3],
            cash_per_share=0,
            bonus_shares_per_share=0.1,
        )

        def decisions(signal_bars, entry_date, entry_price, entry_atr, last_exit_date, *args):
            signal_date = signal_bars[-1].trade_date
            if entry_date is None and signal_date == dates[0]:
                return DefensiveDecision("buy", "test entry", 1, 0.5)
            if entry_date is not None and signal_date == dates[2]:
                return DefensiveDecision("sell", "test exit")
            return evaluate_defensive_mean_reversion(
                signal_bars, entry_date, entry_price, entry_atr, last_exit_date, *args,
            )

        with patch("app.backtest.engine.evaluate_defensive_mean_reversion", side_effect=decisions):
            result = run_backtest(
                bars, [action], params(
                    strategy_code="defensive_mean_reversion", trend_ma_period=21,
                    start_date=dates[0], end_date=dates[-1],
                ),
            )

        self.assertEqual([(trade.side, trade.trade_date, trade.quantity) for trade in result.trades], [
            ("buy", dates[1], 5000),
            ("sell", dates[3], 5000),
            ("sell", dates[4], 500),
        ])
        self.assertFalse(result.open_position)

    def test_skips_order_when_minimum_commission_makes_one_lot_unaffordable(self) -> None:
        bars = [make_bar(day, close) for day, close in enumerate([10, 9, 8, 12, 10], 1)]

        result = run_backtest(
            bars,
            [],
            params(initial_capital=1000, minimum_commission=5),
        )

        self.assertEqual(result.trades, [])
        self.assertEqual(result.equity_curve[-1].cash, 1000)

    def test_rejects_invalid_moving_average_periods(self) -> None:
        with self.assertRaises(ValueError):
            run_backtest([make_bar(day, 10) for day in range(1, 4)], [], params(short_window=3, long_window=3))

    def test_cancel_callback_stops_without_returning_partial_result(self) -> None:
        with self.assertRaises(InterruptedError):
            run_backtest(
                [make_bar(day, 10) for day in range(1, 5)],
                [],
                params(),
                cancel_check=lambda: True,
            )

    def test_unresolved_corporate_action_is_reported_and_not_applied(self) -> None:
        action = CorporateAction(
            symbol="600000",
            record_date=date(2024, 1, 2),
            ex_date=None,
            cash_per_share=1,
            status="unresolved",
        )

        result = run_backtest([make_bar(day, 10) for day in range(1, 4)], [action], params())

        self.assertTrue(result.warnings)
        self.assertEqual(result.dividends_total, 0)


if __name__ == "__main__":
    unittest.main()
