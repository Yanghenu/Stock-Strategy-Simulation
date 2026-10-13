"""基金行情用例：单只标的查询，历史日线和净值由 SQL Server 持久化。"""

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable

from app.config import FundSettings
from app.fund_data.akshare_source import AKShareFundSource, FundMarketSourceError
from app.fund_data.models import (
    FundBar, FundDataError, FundNavPoint, FundQuote, FundType,
)
from app.market_data.normalize import _CHINA_TZ

logger = logging.getLogger(__name__)


class FundMarketDataServiceError(RuntimeError):
    """基金行情查询失败；异常信息不包含本机连接凭据。"""


def normalize_fund_symbol(value: str) -> str:
    """基金代码统一为六位数字，不接受股票市场前缀或其他内容。"""

    symbol = value.strip()
    if len(symbol) != 6 or not symbol.isdigit():
        raise FundDataError("基金代码必须是六位数字")
    return symbol


class FundMarketDataService:
    """组合 AKShare 与 SQL Server；缓存仅用于可丢失的实时展示快照。"""

    def __init__(
        self,
        repository: Any,
        source: AKShareFundSource,
        settings: FundSettings | None = None,
        cache_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._source = source
        self._settings = settings or FundSettings()
        self._clock = cache_clock or (lambda: datetime.now().astimezone())
        self._quotes: dict[tuple[str, str], FundQuote] = {}
        self._bars_cache: dict[tuple[str, str, date, date], list[FundBar]] = {}
        self._bars_cache_times: dict[tuple[str, str, date, date], datetime] = {}
        self._nav_cache: dict[str, list[FundNavPoint]] = {}
        self._nav_cache_times: dict[str, datetime] = {}
        self._distribution_cache: dict[str, list[Any]] = {}
        self._distribution_cache_times: dict[str, datetime] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _type(value: str) -> FundType:
        if value not in {"etf", "lof", "open_end"}:
            raise FundDataError("未知基金类型")
        return value  # type: ignore[return-value]

    def _ttl(self, fund_type: FundType) -> int:
        return self._settings.open_nav_refresh_interval_seconds if fund_type == "open_end" else self._settings.exchange_refresh_interval_seconds

    def search(self, query: str, fund_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        term = query.strip()[:40]
        if not term:
            return []
        result_limit = max(1, min(limit, 200))
        try:
            cached_rows = self._repository.search_funds(term, fund_type, result_limit)
        except Exception as exc:
            logger.exception("读取基金搜索索引失败")
            raise FundMarketDataServiceError("读取基金搜索索引失败，请检查 SQL Server") from exc
        try:
            # 按用户输入的代码或名称查找，并仅把命中标的写入本机索引。
            instruments = self._source.search_instruments(term, self._type(fund_type) if fund_type else None)
            if instruments:
                self._repository.upsert_instruments(instruments)
        except (FundMarketSourceError, FundMarketDataServiceError) as exc:
            logger.warning("基金关键词搜索源暂不可用，尝试使用 SQL Server 索引：%s", term, exc_info=True)
            if cached_rows:
                return cached_rows[:result_limit]
            raise FundMarketDataServiceError("基金关键词搜索暂不可用，请稍后重试") from exc
        # 搜索源提供新结果，SQL 索引补充此前已搜索过的名称，按代码去重。
        merged = {item.symbol: item.__dict__ for item in instruments}
        for row in cached_rows:
            merged.setdefault(row["symbol"], row)
        return list(merged.values())[:result_limit]

    def get_quote(self, fund_type: str, symbol: str, force_refresh: bool = False) -> FundQuote:
        kind = self._type(fund_type)
        code = normalize_fund_symbol(symbol)
        now = self._clock()
        with self._lock:
            cached = self._quotes.get((kind, code))
            if not force_refresh and cached and (now - cached.fetched_at).total_seconds() < self._ttl(kind):
                return cached

        instrument = self._instrument_for_quote(kind, code)
        name = instrument["name"]
        if kind == "open_end":
            points = self.get_nav(kind, code)
            if not points:
                raise FundMarketDataServiceError(f"基金 {code} 暂无已公布净值")
            latest = points[-1]
            previous = points[-2].unit_nav if len(points) > 1 else None
            change = latest.daily_return
            if change is None and previous:
                change = latest.unit_nav / previous - 1
            quote = FundQuote(
                code, name, kind, latest.unit_nav, change or 0, None, None, None, previous,
                0, 0, "AKShare 单只基金已公布净值", now, latest.trade_date,
                latest.accumulated_nav, instrument["subscription_status"],
                instrument["redemption_status"], instrument["fee_description"],
                stale=latest.trade_date < now.astimezone(_CHINA_TZ).date(), category=instrument["category"],
            )
        else:
            trade_day = now.astimezone(_CHINA_TZ).date() if now.tzinfo else now.date()
            intraday = []
            try:
                intraday = self._source.fetch_intraday(kind, code, trade_day)
            except Exception:
                # 分钟源偶发不可用时仍尝试按单只代码读取历史日线。
                logger.warning("基金分钟报价暂不可用，回退单只日线：%s %s", kind, code, exc_info=True)
            try:
                bars = self.get_bars(kind, code, trade_day - timedelta(days=60), trade_day)
            except FundMarketDataServiceError:
                if not intraday:
                    raise
                # 日线接口失败时仍显示已取得的单只分钟报价，涨跌幅留空为零。
                logger.warning("基金日线不可用，保留分钟报价：%s %s", kind, code, exc_info=True)
                bars = []
            if not bars and not intraday:
                raise FundMarketDataServiceError(f"基金 {code} 暂无可用行情")
            if intraday:
                latest_minute = intraday[-1]
                prior = next((bar for bar in reversed(bars) if bar.trade_date < latest_minute.trade_time.date()), None)
                previous_close = prior.close if prior else None
                price = latest_minute.close
                quote = FundQuote(
                    code, name, kind, price,
                    price / previous_close - 1 if previous_close else 0,
                    latest_minute.open, latest_minute.high, latest_minute.low, previous_close,
                    latest_minute.volume, latest_minute.turnover, "AKShare 单只基金分钟行情", now,
                    subscription_status=instrument["subscription_status"],
                    redemption_status=instrument["redemption_status"],
                    fee_description=instrument["fee_description"],
                    stale=latest_minute.trade_time.date() < trade_day, category=instrument["category"],
                )
            else:
                latest_bar = bars[-1]
                previous = bars[-2].close if len(bars) > 1 else None
                quote = FundQuote(
                    code, name, kind, latest_bar.close,
                    latest_bar.close / previous - 1 if previous else 0,
                    latest_bar.open, latest_bar.high, latest_bar.low, previous,
                    latest_bar.volume, latest_bar.turnover, "AKShare 单只基金历史日线", now,
                    subscription_status=instrument["subscription_status"],
                    redemption_status=instrument["redemption_status"],
                    fee_description=instrument["fee_description"],
                    stale=latest_bar.trade_date < trade_day, category=instrument["category"],
                )
        with self._lock:
            self._quotes[(kind, code)] = quote
        return quote

    def _instrument_for_quote(self, fund_type: FundType, symbol: str) -> dict[str, Any]:
        """优先读本机名称索引；缺少记录时只请求该基金的名称元数据。"""

        try:
            rows = self._repository.search_funds(symbol, fund_type, 1)
        except Exception as exc:
            logger.exception("读取基金标的索引失败：%s", symbol)
            raise FundMarketDataServiceError("读取基金标的资料失败，请检查 SQL Server") from exc
        if rows:
            return rows[0]
        try:
            items = self._source.search_instruments(symbol, fund_type)
            item = next((row for row in items if row.symbol == symbol), None)
            if item:
                self._repository.upsert_instruments([item])
                return item.__dict__
        except FundMarketSourceError:
            logger.warning("基金 %s 名称查询失败，将继续尝试行情接口", symbol, exc_info=True)
        # 代码仍可用于后续单只行情查询，名称源不可用时以代码作为临时显示名。
        return {"name": symbol, "subscription_status": "未知", "redemption_status": "未知",
                "fee_description": "", "category": ""}

    def get_bars(self, fund_type: str, symbol: str, start_date: date, end_date: date) -> list[FundBar]:
        kind = self._type(fund_type)
        code = normalize_fund_symbol(symbol)
        if start_date > end_date:
            raise FundDataError("基金历史行情开始日期不能晚于结束日期")
        if kind == "open_end":
            return [FundBar(p.trade_date, p.unit_nav, p.unit_nav, p.unit_nav, p.unit_nav) for p in self.get_nav(kind, code, start_date, end_date)]
        cache_key = (kind, code, start_date, end_date)
        now = self._clock()
        with self._lock:
            cached = self._bars_cache.get(cache_key)
            cached_at = self._bars_cache_times.get(cache_key)
            if cached is not None and cached_at is not None and (now - cached_at).total_seconds() < max(120, self._settings.exchange_refresh_interval_seconds * 6):
                return list(cached)
        try:
            bars = self._source.fetch_bars(kind, code, start_date, end_date)
            if bars:
                self._repository.upsert_bars(kind, code, bars)
                with self._lock:
                    self._bars_cache[cache_key] = list(bars)
                    self._bars_cache_times[cache_key] = now
                return bars
            stored = self._repository.get_bars(kind, code, start_date, end_date)
            if stored:
                with self._lock:
                    self._bars_cache[cache_key] = list(stored)
                    self._bars_cache_times[cache_key] = now
                return stored
        except Exception as exc:
            logger.warning("基金日线源不可用，回退 SQL 历史数据：%s %s", kind, code, exc_info=True)
            try:
                bars = self._repository.get_bars(kind, code, start_date, end_date)
                if bars:
                    with self._lock:
                        self._bars_cache[cache_key] = list(bars)
                        self._bars_cache_times[cache_key] = now
                    return bars
            except Exception:
                logger.exception("读取基金日线回退数据失败")
            raise FundMarketDataServiceError("基金日线暂不可用，暂无本机历史数据可回退") from exc
        return []

    def get_intraday(self, fund_type: str, symbol: str) -> list[Any]:
        kind = self._type(fund_type)
        if kind == "open_end":
            raise FundDataError("开放式基金没有场内分钟行情")
        code = normalize_fund_symbol(symbol)
        try:
            now = self._clock()
            return self._source.fetch_intraday(kind, code, now.astimezone(_CHINA_TZ).date() if now.tzinfo else now.date())
        except Exception as exc:
            logger.exception("读取基金分钟行情失败，fund_type=%s symbol=%s", kind, code)
            raise FundMarketDataServiceError("基金分钟行情暂不可用，请稍后刷新") from exc

    def get_nav(self, fund_type: str, symbol: str, start_date: date | None = None, end_date: date | None = None) -> list[FundNavPoint]:
        kind = self._type(fund_type)
        if kind != "open_end":
            raise FundDataError("只有开放式基金提供单位净值曲线")
        code = normalize_fund_symbol(symbol)
        now = self._clock()
        with self._lock:
            cached = self._nav_cache.get(code)
            cached_at = self._nav_cache_times.get(code)
            if cached is not None and cached_at is not None and (now - cached_at).total_seconds() < self._settings.open_nav_refresh_interval_seconds:
                return self._filter_nav(cached, start_date, end_date)
        try:
            # 取全区间并缓存，调用方可独立筛选日期，避免 worker 每分钟下载整段净值。
            points = self._source.fetch_open_fund_nav(code)
            if points:
                self._repository.upsert_nav_history(kind, code, points)
            else:
                points = self._repository.get_nav_history(kind, code)
        except Exception as exc:
            logger.warning("基金历史净值源暂不可用，尝试读取 SQL Server：%s", code, exc_info=True)
            try:
                points = self._repository.get_nav_history(kind, code)
            except Exception:
                logger.exception("读取基金历史净值失败")
                raise FundMarketDataServiceError("基金历史净值暂不可用") from exc
            if not points:
                raise FundMarketDataServiceError("基金历史净值暂不可用，暂无本机历史数据可回退") from exc
        with self._lock:
            self._nav_cache[code] = points
            self._nav_cache_times[code] = now
        return self._filter_nav(points, start_date, end_date)

    @staticmethod
    def _filter_nav(points: list[FundNavPoint], start_date: date | None, end_date: date | None) -> list[FundNavPoint]:
        return [point for point in points if (start_date is None or point.trade_date >= start_date)
                and (end_date is None or point.trade_date <= end_date)]

    def get_distributions(self, fund_type: str, symbol: str) -> list[Any]:
        """拉取并保存开放式基金分红；场内基金无确认稳定的分红源时返回空列表。"""

        kind = self._type(fund_type)
        code = normalize_fund_symbol(symbol)
        if kind != "open_end":
            return []
        now = self._clock()
        with self._lock:
            cached = self._distribution_cache.get(code)
            cached_at = self._distribution_cache_times.get(code)
            if cached is not None and cached_at is not None and (now - cached_at).total_seconds() < self._settings.open_nav_refresh_interval_seconds:
                return list(cached)
        try:
            items = self._source.fetch_distributions(kind, code)
            if items:
                self._repository.upsert_distributions(kind, code, items)
            else:
                items = self._repository.get_distributions(kind, code)
        except Exception as exc:
            logger.warning("读取开放式基金分红源失败，尝试使用 SQL Server 已保存数据：%s", code, exc_info=True)
            try:
                items = self._repository.get_distributions(kind, code)
            except Exception as db_exc:
                raise FundMarketDataServiceError("基金分红记录暂不可用") from db_exc
        with self._lock:
            self._distribution_cache[code] = list(items)
            self._distribution_cache_times[code] = now
        return list(items)
