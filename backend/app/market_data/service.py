"""行情用例编排；历史数据落 SQL Server，实时快照共享于 Redis。"""

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

from app.cache.redis_cache import CacheUnavailableError
from app.config import Settings
from app.market_data.akshare_source import AKShareSource, MarketDataSourceError
from app.market_data.normalize import _CHINA_TZ
from app.models import Bar, CorporateAction, IntradayBar, Quote
from app.repositories.sql_repository import SqlRepository

logger = logging.getLogger(__name__)
_INTRADAY_FALLBACK_CACHE_SECONDS = 30


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
        # 休市时 2 秒轮询仍需快速探测当天数据，但历史回退只需每 30 秒重新查询一次。
        self._intraday_fallback_cache: dict[str, tuple[float, list[IntradayBar]]] = {}

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

    def get_screening_universe(self, limit: int, minimum_turnover: float) -> list[Quote]:
        """复用完整行情快照，按流动性确定可复现的有界扫描池。"""

        now = self._clock()
        previous = self._cache.get_quotes()
        # 每次创建扫描都重取收盘快照，避免 15:59 的 Redis 快照在 16:00 仍处于 TTL 内。
        quotes = self._refresh_snapshot(previous, now)
        # 外部源故障时可能退回旧快照；此时停止新扫描而不使用过期成交额。
        if not quotes or any(item.stale for item in quotes):
            raise MarketDataServiceError("全市场快照暂不可用或已过期，不能启动选股扫描")
        eligible = [item for item in quotes if item.latest_price > 0 and item.volume_hands > 0
                    and item.turnover >= minimum_turnover and "ST" not in item.name.upper()
                    and len(item.symbol) == 6 and item.symbol.isdigit()]
        eligible.sort(key=lambda item: (-item.turnover, item.symbol))
        return eligible[:limit]

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
        """优先返回当天分钟线；盘前或休市时回退到最近有数据的交易日。"""

        symbol = normalize_symbol(symbol)
        now = self._clock()
        china_now = now.astimezone(_CHINA_TZ) if now.tzinfo else now.replace(tzinfo=_CHINA_TZ)
        trade_date = china_now.date()
        current_error: MarketDataSourceError | None = None
        try:
            bars = self._source.fetch_intraday_bars(symbol, trade_date)
        except MarketDataSourceError as exc:
            current_error = exc

        # 数据源的备用接口可能返回前一交易日；保留实际日期，不能强行改成当天。
        session_bars = self._latest_intraday_session(bars if current_error is None else [], china_now)
        if session_bars:
            return session_bars

        cached = self._intraday_fallback_cache.get(symbol)
        if cached and monotonic() - cached[0] < _INTRADAY_FALLBACK_CACHE_SECONDS:
            return list(cached[1])

        try:
            recent_bars = self._source.fetch_recent_intraday_bars(symbol, trade_date)
        except MarketDataSourceError as exc:
            logger.warning("股票 %s 最近交易日分时回查失败", symbol, exc_info=True)
            if current_error:
                raise MarketDataServiceError("实时分时行情暂时不可用，请稍后刷新") from exc
            raise MarketDataServiceError("最近交易日分时行情暂时不可用，请稍后刷新") from exc

        session_bars = self._latest_intraday_session(recent_bars, china_now)
        self._intraday_fallback_cache[symbol] = (monotonic(), session_bars)
        if session_bars:
            return session_bars
        if current_error:
            raise MarketDataServiceError("实时分时行情暂时不可用，请稍后刷新") from current_error
        return []

    @staticmethod
    def _latest_intraday_session(bars: list[IntradayBar], as_of: datetime) -> list[IntradayBar]:
        """只保留查询时点之前最近一个中国交易日的分钟线，避免图表跨日拼接。"""

        china_now = as_of.astimezone(_CHINA_TZ) if as_of.tzinfo else as_of.replace(tzinfo=_CHINA_TZ)
        available: list[tuple[date, datetime, IntradayBar]] = []
        for bar in bars:
            stamp = bar.trade_time.astimezone(_CHINA_TZ) if bar.trade_time.tzinfo else bar.trade_time.replace(tzinfo=_CHINA_TZ)
            if stamp <= china_now:
                available.append((stamp.date(), stamp, bar))
        if not available:
            return []

        latest_date = max(item[0] for item in available)
        return [item[2] for item in sorted(available, key=lambda item: item[1]) if item[0] == latest_date]

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
