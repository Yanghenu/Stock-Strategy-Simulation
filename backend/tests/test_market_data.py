import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import MarketDataSettings
from app.market_data.akshare_source import AKShareSource, _requests_timeout
from app.market_data.normalize import normalize_actions, normalize_bars, normalize_intraday_bars, normalize_quotes


class MarketDataNormalizationTests(unittest.TestCase):
    def test_normalizes_minute_rows_and_converts_eastmoney_lots_to_shares(self) -> None:
        bars = normalize_intraday_bars([{
            "时间": "2026-10-13 09:31:00", "开盘": 9.6, "最高": 9.7,
            "最低": 9.5, "收盘": 9.65, "成交量": 123, "成交额": 118695,
        }], volume_multiplier=100)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].trade_time.isoformat(), "2026-10-13T09:31:00+08:00")
        self.assertEqual((bars[0].open, bars[0].high, bars[0].low, bars[0].close), (9.6, 9.7, 9.5, 9.65))
        self.assertEqual(bars[0].volume, 12300)
        self.assertEqual(bars[0].turnover, 118695)

    def test_merges_unadjusted_and_qfq_bars_by_date(self) -> None:
        raw = [{
            "日期": "2024-01-02", "开盘": 10, "最高": 11, "最低": 9,
            "收盘": 10.5, "成交量": 1234, "成交额": 123400,
        }]
        adjusted = [{
            "日期": "2024-01-02", "开盘": 8, "最高": 9, "最低": 7,
            "收盘": 8.5, "成交量": 1234, "成交额": 123400,
        }]

        bars = normalize_bars("600000", raw, adjusted)

        self.assertEqual(bars[0].close, 10.5)
        self.assertEqual(bars[0].adj_close, 8.5)
        self.assertEqual(bars[0].volume, 1234)

    def test_rejects_duplicate_dates_or_missing_qfq_prices(self) -> None:
        raw = [{"日期": "2024-01-02", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10}]

        with self.assertRaises(ValueError):
            normalize_bars("600000", [*raw, *raw], raw)
        with self.assertRaises(ValueError):
            normalize_bars("600000", raw, [])

    def test_maps_spot_quotes_and_skips_invalid_prices(self) -> None:
        fetched_at = datetime(2024, 1, 2, 1, 30, tzinfo=timezone.utc)
        quotes = normalize_quotes([
            {
                "代码": "600000", "名称": "浦发银行", "最新价": 8.2,
                "涨跌额": 0.1, "涨跌幅": 1.23, "今开": 8.1,
                "最高": 8.3, "最低": 8.0, "昨收": 8.1,
                "成交量": 100, "成交额": 820000,
            },
            {"代码": "600001", "名称": "无行情", "最新价": "-"},
        ], fetched_at)

        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].symbol, "600000")
        self.assertEqual(quotes[0].volume_hands, 100)
        self.assertEqual(quotes[0].fetched_at, fetched_at)

    def test_converts_cninfo_per_ten_share_dividend_and_bonus_units(self) -> None:
        actions, warnings = normalize_actions("600000", [{
            "实施方案公告日期": "2024-01-01",
            "股权登记日": "2024-01-05",
            "除权日": "2024-01-08",
            "派息比例": 2,
            "送股比例": 1,
            "转增比例": 0,
        }])

        self.assertEqual(warnings, [])
        self.assertEqual(actions[0].cash_per_share, 0.2)
        self.assertEqual(actions[0].bonus_shares_per_share, 0.1)

    def test_marks_unresolved_corporate_action_instead_of_fabricating_date(self) -> None:
        actions, warnings = normalize_actions("600000", [{
            "股权登记日": "2024-01-05", "除权日": "-", "派息比例": 2,
            "送股比例": 0, "转增比例": 0,
        }])

        self.assertIsNone(actions[0].ex_date)
        self.assertEqual(actions[0].status, "unresolved")
        self.assertTrue(warnings)

    def test_akshare_adapter_uses_daily_qfq_calls_and_retries_transient_errors(self) -> None:
        raw = [{"日期": "2024-01-02", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10}]

        class Provider:
            calls: list[dict[str, object]] = []
            quote_attempts: int = 0

            def stock_zh_a_hist(self, **kwargs: object):
                self.calls.append(kwargs)
                return raw

            def stock_zh_a_spot_tx(self):
                self.quote_attempts += 1
                if self.quote_attempts == 1:
                    raise RuntimeError("temporary upstream failure")
                return [{
                    "code": "sh600000", "name": "浦发银行", "zxj": 9.66,
                    "zd": 0.12, "zdf": 1.26, "volume": 506314,
                    "turnover": 48914,
                }]

        provider = Provider()
        source = AKShareSource(MarketDataSettings(5, 1), provider=provider)

        bars = source.fetch_bars("600000", date(2024, 1, 1), date(2024, 1, 31))
        quotes = source.fetch_quotes()

        self.assertEqual([call["adjust"] for call in provider.calls], ["", "qfq"])
        self.assertEqual(bars[0].close, 10)
        self.assertEqual(provider.quote_attempts, 2)
        self.assertEqual(quotes[0].symbol, "600000")
        self.assertEqual(quotes[0].source, "AKShare 腾讯")

    def test_history_falls_back_to_tencent_when_eastmoney_fails(self) -> None:
        class Provider:
            tx_calls: list[dict[str, object]] = []

            def stock_zh_a_hist(self, **kwargs: object):
                raise RuntimeError("Eastmoney unavailable")

            def stock_zh_a_hist_tx(self, **kwargs: object):
                self.tx_calls.append(kwargs)
                adjusted = kwargs["adjust"] == "qfq"
                return [{
                    "date": "2024-01-02",
                    "open": 8 if adjusted else 10,
                    "high": 9 if adjusted else 11,
                    "low": 7 if adjusted else 9,
                    "close": 8.5 if adjusted else 10.5,
                    "volume": 1234,
                    "amount": 123400,
                }]

        provider = Provider()
        source = AKShareSource(MarketDataSettings(5, 0), provider=provider)

        bars = source.fetch_bars("600000", date(2024, 1, 1), date(2024, 1, 31))

        self.assertEqual([call["adjust"] for call in provider.tx_calls], ["", "qfq"])
        self.assertEqual((bars[0].close, bars[0].adj_close, bars[0].volume), (10.5, 8.5, 1234))

    def test_intraday_falls_back_to_sina_with_market_prefixed_symbol(self) -> None:
        class Provider:
            sina_calls: list[dict[str, object]] = []

            def stock_zh_a_hist_min_em(self, **kwargs: object):
                raise RuntimeError("Eastmoney minute endpoint unavailable")

            def stock_zh_a_minute(self, **kwargs: object):
                self.sina_calls.append(kwargs)
                return [{
                    "day": "2026-10-13 09:31:00", "open": 9.6, "high": 9.7,
                    "low": 9.5, "close": 9.65, "volume": 12300, "amount": 118695,
                }]

        provider = Provider()
        source = AKShareSource(MarketDataSettings(5, 0), provider=provider)

        bars = source.fetch_intraday_bars("600000", date(2026, 10, 13))

        self.assertEqual(provider.sina_calls, [{"symbol": "sh600000", "period": "1", "adjust": ""}])
        self.assertEqual(bars[0].trade_time.isoformat(), "2026-10-13T09:31:00+08:00")
        self.assertEqual((bars[0].close, bars[0].volume), (9.65, 12300))

    def test_intraday_uses_eastmoney_one_minute_bars_and_lot_volume(self) -> None:
        class Provider:
            calls: list[dict[str, object]] = []

            def stock_zh_a_hist_min_em(self, **kwargs: object):
                self.calls.append(kwargs)
                return [{
                    "时间": "2026-10-13 09:31:00", "开盘": 9.6, "最高": 9.7,
                    "最低": 9.5, "收盘": 9.65, "成交量": 123, "成交额": 118695,
                }]

            def stock_zh_a_minute(self, **kwargs: object):
                raise AssertionError("fallback should not run when Eastmoney returns bars")

        provider = Provider()
        source = AKShareSource(MarketDataSettings(5, 0), provider=provider)

        bars = source.fetch_intraday_bars("600000", date(2026, 10, 13))

        self.assertEqual(provider.calls, [{
            "symbol": "600000", "start_date": "2026-10-13 09:15:00",
            "end_date": "2026-10-13 15:00:00", "period": "1", "adjust": "",
        }])
        self.assertEqual((bars[0].close, bars[0].volume), (9.65, 12300))

    def test_recent_intraday_reads_a_bounded_window_for_closed_days(self) -> None:
        class Provider:
            calls: list[dict[str, object]] = []

            def stock_zh_a_hist_min_em(self, **kwargs: object):
                self.calls.append(kwargs)
                return [
                    {"时间": "2026-10-15 15:00:00", "开盘": 10, "最高": 10, "最低": 10, "收盘": 10, "成交量": 1, "成交额": 10},
                    {"时间": "2026-10-16 15:00:00", "开盘": 11, "最高": 11, "最低": 11, "收盘": 11, "成交量": 1, "成交额": 11},
                ]

            def stock_zh_a_minute(self, **kwargs: object):
                raise AssertionError("fallback should not run when the recent range contains bars")

        provider = Provider()
        source = AKShareSource(MarketDataSettings(5, 0), provider=provider)

        bars = source.fetch_recent_intraday_bars("600000", date(2026, 10, 18))

        self.assertEqual(provider.calls, [{
            "symbol": "600000", "start_date": "2026-10-04 09:15:00",
            "end_date": "2026-10-18 15:00:00", "period": "1", "adjust": "",
        }])
        self.assertEqual([bar.trade_time.date() for bar in bars], [date(2026, 10, 15), date(2026, 10, 16)])

    def test_intraday_does_not_call_sina_when_eastmoney_has_no_current_day_rows(self) -> None:
        class Provider:
            sina_called = False

            def stock_zh_a_hist_min_em(self, **kwargs: object):
                return []

            def stock_zh_a_minute(self, **kwargs: object):
                self.sina_called = True
                return []

        provider = Provider()
        source = AKShareSource(MarketDataSettings(5, 0), provider=provider)

        bars = source.fetch_intraday_bars("600000", date(2026, 10, 18))

        self.assertEqual(bars, [])
        self.assertFalse(provider.sina_called)

    def test_maps_tencent_snapshot_quote_fields_and_marks_missing_ohlc(self) -> None:
        class Provider:
            def stock_zh_a_spot_tx(self):
                return [{
                    "code": "sh600000", "name": "浦发银行", "zxj": "9.66",
                    "zd": "0.12", "zdf": "1.26", "volume": "506314",
                    "turnover": "48914",
                }]

        provider = Provider()
        source = AKShareSource(MarketDataSettings(5, 0), provider=provider)

        quote = source.fetch_quote("600000")

        self.assertEqual((quote.name, quote.latest_price, quote.volume_hands), ("浦发银行", 9.66, 506314))
        self.assertEqual((quote.change_amount, quote.change_percent), (0.12, 1.26))
        self.assertAlmostEqual(quote.previous_close, 9.54)
        self.assertEqual(quote.turnover, 489140000)
        self.assertEqual((quote.open, quote.high, quote.low), (0, 0, 0))
        self.assertEqual(quote.source, "AKShare 腾讯")

    def test_requests_timeout_replaces_explicit_none_and_restores_original(self) -> None:
        with patch("requests.sessions.Session.request") as original:
            with _requests_timeout(7):
                import requests
                requests.sessions.Session.request(object(), "GET", "https://example.com", timeout=None)

            self.assertEqual(original.call_args.kwargs["timeout"], 7)
            self.assertIs(requests.sessions.Session.request, original)


if __name__ == "__main__":
    unittest.main()
