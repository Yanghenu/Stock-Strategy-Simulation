"""行情用例编排；历史数据落 SQL Server，实时快照共享于 Redis。"""

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from app.cache.redis_cache import CacheUnavailableError
from app.config import Settings
from app.market_data.akshare_source import AKShareSource, MarketDataSourceError
from app.market_data.normalize import _CHINA_TZ
from app.models import Bar, CorporateAction, IntradayBar, Quote
from app.repositories.sql_repository import SqlRepository

logger = logging.getLogger(__name__)


class MarketDataServiceError(RuntimeError):
    """行情查询无法完成，包含可安全返回给本机页面的说明。"""


def normalize_symbol(value: str) -> str:
    """接受六位 A 股代码以及常见交易所前缀，内部统一为六位数字。"""

    symbol = value.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if symbol.startswith(prefix):
            symbol = symbol[2:]
            break
    if len(symbol) != 6 or not symbol.isdigit():
        raise MarketDataServiceError("股票代码必须是六位数字")
    return symbol


def _exchange(symbol: str) -> str:
    if symbol.startswith(("60", "68", "90")):
        return "SH"
    if symbol.startswith(("00", "30")):
        return "SZ"
    return "BJ"


class MarketDataService:
    """统一处理行情缓存、外部频率限制和历史区间持久化。"""

    def __init__(
        self,
        repository: SqlRepository,
        source: AKShareSource,
        cache: Any,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._source = source
        self._cache = cache
        self._settings = settings
        self._clock = clock or (lambda: datetime.now().astimezone())

    def get_quote(self, symbol: str, force_refresh: bool = False) -> Quote:
        """从共享腾讯快照读取单股行情；手动刷新时更新一次全市场快照。"""

        symbol = normalize_symbol(symbol)
        now = self._clock()
        cached = self._cache.get_quote(symbol)
        if not force_refresh and cached and (now - cached.fetched_at).total_seconds() < self._settings.redis.refresh_interval_seconds:
            return replace(cached, stale=False)

        token = uuid4().hex
        if not self._cache.acquire_quote_refresh_lock(symbol, token):
            latest = self._cache.get_quote(symbol) or cached
            if latest:
                stale = (now - latest.fetched_at).total_seconds() >= self._settings.redis.refresh_interval_seconds
                return replace(latest, stale=stale)
            raise MarketDataServiceError("该股票的行情刷新正在进行，请稍后重试")
        try:
            quotes = self._refresh_snapshot(self._cache.get_quotes(), now)
            refreshed = next((item for item in quotes if item.symbol == symbol), None)
            if refreshed:
                return refreshed
            if cached:
                return replace(cached, stale=True)
            raise MarketDataServiceError("实时行情源没有返回该股票，且 Redis 中没有可回退的旧报价")
        except MarketDataServiceError as exc:
            if cached:
                return replace(cached, stale=True)
            raise MarketDataServiceError("实时行情暂不可用，Redis 中没有可回退的旧报价") from exc
        finally:
            self._cache.release_quote_refresh_lock(symbol, token)

    def search_stocks(self, query: str, limit: int = 20) -> list[Quote]:
        """从共享的全市场快照按代码或名称筛选，不额外请求股票列表接口。"""

        now = self._clock()
        quotes = self._cache.get_quotes()
        expired = not quotes or (now - quotes[0].fetched_at).total_seconds() >= self._settings.redis.refresh_interval_seconds
        if expired:
            quotes = self._refresh_snapshot(quotes, now)
        term = query.strip().casefold()
        matches = [item for item in quotes if not term or term in item.symbol or term in item.name.casefold()]
        return matches[: max(1, min(limit, 100))]

    def _refresh_snapshot(self, previous: list[Quote], now: datetime) -> list[Quote]:
        """通过 Redis 短租约合并并发刷新，只有整份快照成功才发布。"""

        token = uuid4().hex
        if not self._cache.acquire_refresh_lock(token):
            latest = self._cache.get_quotes()
            current = latest or previous
            interval = self._settings.redis.refresh_interval_seconds
            return [
                replace(item, stale=(now - item.fetched_at).total_seconds() >= interval)
                for item in current
            ]
        try:
            try:
                fetched_at = self._clock()
                quotes = [replace(item, fetched_at=fetched_at, stale=False) for item in self._source.fetch_quotes()]
                if not quotes:
                    raise MarketDataSourceError("全市场行情没有有效记录")
                self._cache.publish_snapshot(quotes, fetched_at)
                return quotes
            except MarketDataSourceError:
                if previous:
                    return [replace(item, stale=True) for item in previous]
                raise MarketDataServiceError("实时行情暂不可用，Redis 中没有可回退的旧快照")
        finally:
            self._cache.release_refresh_lock(token)

    def get_bars(self, symbol: str, start_date: date, end_date: date) -> list[Bar]:
        """优先读 SQL Server 的完整覆盖区间，缺失时拉取并写入标准日线。"""

        symbol = normalize_symbol(symbol)
        if start_date > end_date:
            raise MarketDataServiceError("历史行情开始日期不能晚于结束日期")
        if self._repository.has_bar_coverage(symbol, start_date, end_date):
            return self._repository.get_bars(symbol, start_date, end_date)

        bars = self._source.fetch_bars(symbol, start_date, end_date)
        if not bars:
            raise MarketDataServiceError(f"{symbol} 在所选日期范围内没有历史行情")
        quote = None
        try:
            quote = self._cache.get_quote(symbol)
        except CacheUnavailableError:
            # 实时缓存故障不应阻断从 AKShare 获取日线并执行历史回测。
            logger.warning("Redis 不可用，股票基础名称暂用代码 %s", symbol)
        self._repository.upsert_stock(symbol, quote.name if quote else symbol, _exchange(symbol))
        self._repository.upsert_bars(symbol, bars)
        self._repository.mark_bar_coverage(symbol, start_date, end_date)
        return bars

    def get_intraday_bars(self, symbol: str) -> list[IntradayBar]:
        """只返回中国时区当天的分钟线，盘前和休市时允许空结果。"""

        symbol = normalize_symbol(symbol)
        now = self._clock()
        trade_date = now.astimezone(_CHINA_TZ).date() if now.tzinfo else now.date()
        try:
            bars = self._source.fetch_intraday_bars(symbol, trade_date)
        except MarketDataSourceError as exc:
            raise MarketDataServiceError("实时分时行情暂时不可用，请稍后刷新") from exc
        return sorted(
            (bar for bar in bars if bar.trade_time.astimezone(_CHINA_TZ).date() == trade_date),
            key=lambda bar: bar.trade_time,
        )

    def ensure_stock(self, symbol: str) -> None:
        """回测入队前满足 SQL 外键；Redis 暂不可用时仍允许按代码建基础行。"""

        symbol = normalize_symbol(symbol)
        name = symbol
        try:
            quote = self._cache.get_quote(symbol)
            if quote:
                name = quote.name
        except CacheUnavailableError:
            logger.warning("Redis 不可用，股票基础名称暂用代码 %s", symbol)
        self._repository.upsert_stock(symbol, name, _exchange(symbol))

    def get_backtest_inputs(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        long_window: int,
    ) -> tuple[list[Bar], list[CorporateAction], list[str]]:
        """额外加载均线预热日线，并将实施/未确认权益事件保存为结构化字段。"""

        symbol = normalize_symbol(symbol)
        warmup_days = max(60, long_window * 3)
        data_start = start_date - timedelta(days=warmup_days)
        bars = self.get_bars(symbol, data_start, end_date)
        incoming_actions, warnings = self._source.fetch_actions(symbol)
        self._repository.upsert_actions(symbol, incoming_actions)
        actions = self._repository.get_actions(symbol, data_start, end_date)
        return bars, actions, warnings
