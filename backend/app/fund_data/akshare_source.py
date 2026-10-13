"""AKShare 基金接口适配层；统一有限重试、超时和来源字段。"""

import logging
import time
from datetime import date, datetime
from typing import Any

import requests

from app.config import MarketDataSettings
from app.fund_data.models import FundBar, FundDataError, FundDistribution, FundInstrument, FundType
from app.fund_data.normalize import (
    normalize_bars, normalize_distributions, normalize_intraday, normalize_nav,
    normalize_open_fund_snapshot, normalize_spot,
)
from app.market_data.akshare_source import _requests_timeout

logger = logging.getLogger(__name__)


class FundMarketSourceError(RuntimeError):
    """基金上游接口没有返回可用数据。"""


class AKShareFundSource:
    """基金行情源；provider 参数便于离线替换外部 AKShare 调用。"""

    _SPOT_METHOD = {"etf": "fund_etf_spot_em", "lof": "fund_lof_spot_em"}
    _HIST_METHOD = {"etf": "fund_etf_hist_em", "lof": "fund_lof_hist_em"}
    _MIN_METHOD = {"etf": "fund_etf_hist_min_em", "lof": "fund_lof_hist_min_em"}

    def __init__(self, settings: MarketDataSettings, provider: Any | None = None, retry_delay_seconds: float = 0.2) -> None:
        self._settings = settings
        self._provider = provider
        self._retry_delay = max(0, retry_delay_seconds)

    def _akshare(self) -> Any:
        if self._provider is None:
            try:
                import akshare
            except ImportError as exc:
                raise FundMarketSourceError("缺少 AKShare，请按 requirements.txt 安装后重试") from exc
            self._provider = akshare
        return self._provider

    def _call(self, method_name: str, **kwargs: object) -> Any:
        """接口调用最多重试配置次数，最终异常通过异常链保留供日志排查。"""

        method = getattr(self._akshare(), method_name, None)
        if method is None:
            raise FundMarketSourceError(f"当前 AKShare 版本不包含接口 {method_name}")
        for attempt in range(self._settings.max_retries + 1):
            try:
                with _requests_timeout(self._settings.request_timeout_seconds):
                    return method(**kwargs)
            except Exception as exc:
                if attempt >= self._settings.max_retries:
                    logger.exception("AKShare 基金接口 %s 调用失败", method_name)
                    raise FundMarketSourceError(f"基金行情接口 {method_name} 暂时不可用") from exc
                logger.warning("AKShare 基金接口 %s 失败，重试 %d/%d", method_name, attempt + 1, self._settings.max_retries)
                if self._retry_delay:
                    time.sleep(min(self._retry_delay * (attempt + 1), 1))
        raise FundMarketSourceError(f"基金行情接口 {method_name} 暂时不可用")

    def search_instruments(self, query: str, fund_type: FundType | None = None) -> list[FundInstrument]:
        """通过基金关键词搜索接口查单只结果，避免为搜索下载全市场行情表。"""

        term = query.strip()[:40]
        if not term:
            return []
        url = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
        payload: dict[str, Any] | None = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                response = requests.get(
                    url,
                    params={"m": "1", "key": term, "callback": ""},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"},
                    timeout=self._settings.request_timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:
                if attempt >= self._settings.max_retries:
                    logger.exception("基金关键词搜索失败")
                    raise FundMarketSourceError("基金关键词搜索接口暂时不可用") from exc
                logger.warning("基金关键词搜索失败，重试 %d/%d", attempt + 1, self._settings.max_retries)
                if self._retry_delay:
                    time.sleep(min(self._retry_delay * (attempt + 1), 1))

        results: list[FundInstrument] = []
        for row in (payload or {}).get("Datas") or []:
            # 搜索接口可能同时返回基金公司等条目，只接收 CATEGORY=700 的基金。
            if str(row.get("CATEGORY")) != "700":
                continue
            symbol = str(row.get("CODE") or row.get("_id") or "").strip()
            if len(symbol) != 6 or not symbol.isdigit():
                continue
            kind = self._fund_type_for_symbol(symbol)
            if fund_type and kind != fund_type:
                continue
            base = row.get("FundBaseInfo") or {}
            name = str(row.get("NAME") or base.get("SHORTNAME") or "").strip()
            if not name:
                continue
            results.append(FundInstrument(
                symbol=symbol,
                name=name,
                fund_type=kind,
                subscription_status=self._status(base.get("ISBUY"), "开放申购", "暂停申购"),
                redemption_status=self._status(base.get("ISRED"), "开放赎回", "暂停赎回"),
                category=str(base.get("FTYPE") or ""),
            ))
        return results

    @staticmethod
    def _fund_type_for_symbol(symbol: str) -> FundType:
        """按场内基金代码段区分 ETF/LOF，其余归为开放式基金。"""

        if symbol.startswith(("159", "508", "510", "511", "512", "513", "515", "516", "517", "518", "520", "560", "561", "562", "563", "588", "589")):
            return "etf"
        if symbol.startswith(("16", "501", "502")):
            return "lof"
        return "open_end"

    @staticmethod
    def _status(value: object, opened: str, closed: str) -> str:
        """把搜索接口的状态码转换成界面可读文本。"""

        if str(value) == "1":
            return opened
        if str(value) == "0":
            return closed
        return "未知"

    def fetch_spot(self, fund_type: FundType):
        if fund_type not in self._SPOT_METHOD:
            raise FundDataError("开放式基金应读取已公布单位净值")
        method = self._SPOT_METHOD[fund_type]
        try:
            rows = self._call(method)
            # 新浪实时行情的成交量已是“股”，东财 ETF/LOF 快照沿用“手”。
            multiplier = 1 if method == "fund_etf_category_sina" else 100
            return normalize_spot(rows, fund_type, datetime.now().astimezone(), multiplier)
        except (FundMarketSourceError, FundDataError):
            # 新浪分类行情同时覆盖 ETF 与 LOF，可作为东方财富快照的备用源。
            category = "ETF基金" if fund_type == "etf" else "LOF基金"
            rows = self._call("fund_etf_category_sina", symbol=category)
            return normalize_spot(rows, fund_type, datetime.now().astimezone(), volume_multiplier=1)

    def fetch_bars(self, fund_type: FundType, symbol: str, start_date: date, end_date: date):
        if start_date > end_date:
            raise FundDataError("基金历史行情开始日期不能晚于结束日期")
        if fund_type == "open_end":
            points = [p for p in self.fetch_open_fund_nav(symbol) if start_date <= p.trade_date <= end_date]
            return [
                # 开放式基金没有场内 OHLC；用每个公布单位净值构造收盘序列供策略使用。
                FundBar(p.trade_date, p.unit_nav, p.unit_nav, p.unit_nav, p.unit_nav) for p in points
            ]
        method = self._HIST_METHOD.get(fund_type)
        if not method:
            raise FundDataError("未知基金类型")
        args = {
            "symbol": symbol, "period": "daily",
            "start_date": start_date.strftime("%Y%m%d"), "end_date": end_date.strftime("%Y%m%d"),
            "adjust": "",
        }
        try:
            # 东方财富日线接口优先；LOF 仅在已安装版本暴露对应函数时尝试。
            if fund_type == "lof" and not hasattr(self._akshare(), method):
                raise FundMarketSourceError("当前 AKShare 版本未提供 LOF 东财日线")
            rows = self._call(method, **args)
            bars = normalize_bars(rows)
            if bars:
                return [bar for bar in bars if start_date <= bar.trade_date <= end_date]
            raise FundMarketSourceError(f"{symbol} 没有可用基金历史行情")
        except (FundMarketSourceError, FundDataError):
            # 官方新浪日线接口同时覆盖 ETF/LOF；代码带交易所前缀，成交量按手换算。
            rows = self._call("fund_etf_hist_sina", symbol=self._sina_symbol(symbol))
            return [bar for bar in normalize_bars(rows) if start_date <= bar.trade_date <= end_date]

    def fetch_intraday(self, fund_type: FundType, symbol: str, trade_date: date):
        method = self._MIN_METHOD.get(fund_type)
        if not method:
            raise FundDataError("开放式基金没有场内分钟行情")
        args = {
            "symbol": symbol, "period": "1", "adjust": "",
            "start_date": f"{trade_date.isoformat()} 09:15:00",
            "end_date": f"{trade_date.isoformat()} 15:00:00",
        }
        try:
            if fund_type == "lof" and not hasattr(self._akshare(), method):
                raise FundMarketSourceError("当前 AKShare 版本未提供 LOF 分钟接口")
            rows = self._call(method, **args)
        except FundMarketSourceError:
            # 个别 AKShare 版本让 ETF 分钟接口同时接受 LOF 代码，尝试后仍会校验数据。
            if fund_type != "lof":
                raise
            rows = self._call("fund_etf_hist_min_em", **args)
        return [bar for bar in normalize_intraday(rows) if bar.trade_time.date() == trade_date]

    def fetch_open_fund_snapshot(self):
        rows = self._call("fund_open_fund_daily_em")
        try:
            categories = self._call("fund_name_em")
        except FundMarketSourceError:
            # 分类表短暂不可用时仍显示已公布净值，实验创建会要求类别已确认。
            logger.warning("基金类别快照暂不可用，保留净值行情但无法分类")
            categories = None
        return normalize_open_fund_snapshot(rows, datetime.now().astimezone(), categories)

    def fetch_open_fund_nav(self, symbol: str):
        rows = self._call("fund_open_fund_info_em", symbol=symbol, indicator="单位净值走势")
        try:
            accumulated_rows = self._call("fund_open_fund_info_em", symbol=symbol, indicator="累计净值走势")
        except FundMarketSourceError:
            logger.warning("基金 %s 的累计净值历史暂不可用，保留单位净值曲线", symbol)
            accumulated_rows = None
        return normalize_nav(rows, accumulated_rows)

    def fetch_distributions(self, fund_type: FundType, symbol: str) -> list[FundDistribution]:
        """读取 AKShare 暴露的分红送配表；接口不存在时不伪造分红记录。"""

        if fund_type != "open_end":
            # AKShare 公募基金历史分红文档当前只提供开放式基金详情接口。
            return []
        try:
            rows = self._call("fund_open_fund_info_em", symbol=symbol, indicator="分红送配详情")
            return normalize_distributions(rows)
        except (FundMarketSourceError, FundDataError):
            logger.warning("AKShare 暂未提供可用的 %s 基金分红数据", fund_type, exc_info=True)
            return []

    @staticmethod
    def _sina_symbol(symbol: str) -> str:
        """新浪基金接口要求带交易所前缀；按场内基金代码段推断沪深市场。"""

        if symbol.startswith(("50", "51", "56", "58", "5", "60", "68", "90")):
            return f"sh{symbol}"
        return f"sz{symbol}"
