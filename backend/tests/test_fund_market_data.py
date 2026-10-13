import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import MarketDataSettings
from app.fund_data.akshare_source import AKShareFundSource
from app.fund_data.models import FundDataError
from app.fund_data.normalize import normalize_bars, normalize_open_fund_snapshot, normalize_spot


class FundMarketDataTests(unittest.TestCase):
    def test_keyword_search_returns_only_matching_fund_instruments(self):
        response = Mock()
        response.json.return_value = {"Datas": [
            {"CATEGORY": 700, "CODE": "510300", "NAME": "沪深300ETF", "FundBaseInfo": {
                "FTYPE": "指数型-股票", "ISBUY": "1", "ISRED": "0",
            }},
            {"CATEGORY": 150, "CODE": "000001", "NAME": "平安银行"},
        ]}
        source = AKShareFundSource(MarketDataSettings(2, 0), retry_delay_seconds=0)

        with patch("app.fund_data.akshare_source.requests.get", return_value=response) as get:
            rows = source.search_instruments("沪深300ETF", "etf")

        self.assertEqual([(row.symbol, row.name, row.fund_type) for row in rows], [
            ("510300", "沪深300ETF", "etf"),
        ])
        self.assertEqual(rows[0].subscription_status, "开放申购")
        self.assertEqual(rows[0].redemption_status, "暂停赎回")
        self.assertEqual(get.call_args.kwargs["params"]["key"], "沪深300ETF")

    def test_spot_keeps_fund_type_converts_percent_and_exchange_volume_to_shares(self):
        rows = [
            {"代码": "510300", "名称": "沪深300ETF", "最新价": "4.12", "涨跌幅": "1.25",
             "今开": "4.08", "最高": "4.13", "最低": "4.07", "昨收": "4.07",
             "成交量": "123", "成交额": "10000"},
            {"代码": "510301", "名称": "无效报价", "最新价": "-", "涨跌幅": "-"},
        ]

        quotes = normalize_spot(rows, "etf")

        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].symbol, "510300")
        self.assertEqual(quotes[0].fund_type, "etf")
        self.assertEqual(quotes[0].change_percent, 0.0125)
        self.assertEqual(quotes[0].volume, 12300)

    def test_open_fund_snapshot_uses_published_nav_and_preserves_status(self):
        row = {"基金代码": "000001", "基金简称": "华夏成长", "日期": "2026-10-12",
               "单位净值": "1.234", "累计净值": "2.345", "日增长率": "0.50%",
               "申购状态": "开放申购", "赎回状态": "开放赎回", "手续费": "0.15%"}

        instruments, quotes = normalize_open_fund_snapshot([row], categories_rows=[
            {"基金代码": "000001", "基金简称": "华夏成长", "基金类型": "混合型"},
        ])

        self.assertEqual(instruments[0].fund_type, "open_end")
        self.assertEqual(quotes[0].latest_price, 1.234)
        self.assertEqual(quotes[0].nav_date, date(2026, 10, 12))
        self.assertEqual(quotes[0].change_percent, 0.005)
        self.assertEqual(quotes[0].subscription_status, "开放申购")
        self.assertEqual(instruments[0].category, "混合型")

    def test_sina_lof_quote_prefix_and_share_volume_are_normalized(self):
        quote = normalize_spot([{
            "代码": "sz161130", "名称": "纳指LOF", "最新价": 1.5, "涨跌幅": 1.2,
            "今开": 1.49, "最高": 1.51, "最低": 1.48, "昨收": 1.48,
            "成交量": 12345, "成交额": 18517,
        }], "lof", volume_multiplier=1)[0]

        self.assertEqual(quote.symbol, "161130")
        self.assertEqual(quote.volume, 12345)

    def test_bars_reject_duplicate_and_out_of_order_dates(self):
        rows = [
            {"日期": "2026-10-12", "开盘": 1, "最高": 2, "最低": 1, "收盘": 1.5, "成交量": 10, "成交额": 20},
            {"日期": "2026-10-11", "开盘": 1, "最高": 2, "最低": 1, "收盘": 1.5, "成交量": 10, "成交额": 20},
        ]

        with self.assertRaises(FundDataError):
            normalize_bars(rows)

    def test_source_routes_by_fund_type_and_retries_before_reraising(self):
        class Provider:
            def __init__(self):
                self.calls = []
                self.attempts = 0

            def fund_etf_spot_em(self):
                self.calls.append(("etf_spot", ()))
                return []

            def fund_lof_spot_em(self):
                self.calls.append(("lof_spot", ()))
                return []

            def fund_etf_hist_em(self, **kwargs):
                self.calls.append(("etf_hist", kwargs))
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("temporary")
                return [{"日期": "2026-10-12", "开盘": 1, "最高": 2, "最低": 1, "收盘": 1.5,
                         "成交量": 10, "成交额": 20}]

        provider = Provider()
        source = AKShareFundSource(MarketDataSettings(1, 1), provider, retry_delay_seconds=0)

        source.fetch_spot("etf")
        source.fetch_spot("lof")
        bars = source.fetch_bars("etf", "510300", date(2026, 10, 1), date(2026, 10, 12))

        self.assertEqual([call[0] for call in provider.calls], ["etf_spot", "lof_spot", "etf_hist", "etf_hist"])
        self.assertEqual(provider.calls[2][1]["symbol"], "510300")
        self.assertEqual(bars[0].trade_date, date(2026, 10, 12))
        with self.assertRaises(FundDataError):
            source.fetch_intraday("open_end", "000001", date(2026, 10, 12))

    def test_source_uses_sina_when_lof_eastmoney_quote_is_unavailable(self):
        class Provider:
            def fund_lof_spot_em(self):
                raise RuntimeError("LOF eastmoney unavailable")

            def fund_etf_category_sina(self, symbol):
                self.category = symbol
                return [{"代码": "sz161130", "名称": "纳指LOF", "最新价": 1.5, "涨跌幅": 1.2,
                         "昨收": 1.48, "今开": 1.49, "最高": 1.51, "最低": 1.48,
                         "成交量": 12345, "成交额": 18517}]

        provider = Provider()
        source = AKShareFundSource(MarketDataSettings(1, 0), provider, retry_delay_seconds=0)
        rows = source.fetch_spot("lof")

        self.assertEqual(provider.category, "LOF基金")
        self.assertEqual(rows[0].symbol, "161130")
        self.assertEqual(rows[0].volume, 12345)

    def test_source_merges_published_cumulative_nav_and_parses_distribution_amount(self):
        class Provider:
            def fund_open_fund_info_em(self, symbol, indicator):
                if indicator == "单位净值走势":
                    return [{"净值日期": "2026-10-12", "单位净值": 1.2, "日增长率": 1.0}]
                if indicator == "累计净值走势":
                    return [{"净值日期": "2026-10-12", "累计净值": 2.35}]
                if indicator == "分红送配详情":
                    return [{"除息日": "2026-10-01", "每份分红": "每份派现金0.0050元"}]
                raise AssertionError(indicator)

        source = AKShareFundSource(MarketDataSettings(1, 0), Provider(), retry_delay_seconds=0)
        points = source.fetch_open_fund_nav("000001")
        distributions = source.fetch_distributions("open_end", "000001")

        self.assertEqual(points[0].accumulated_nav, 2.35)
        self.assertEqual(points[0].daily_return, 0.01)
        self.assertEqual(distributions[0].cash_per_unit, 0.005)


if __name__ == "__main__":
    unittest.main()
