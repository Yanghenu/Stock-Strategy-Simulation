"""Redis 实时行情快照；每只股票以字段 Hash 保存，整份版本写完才切换指针。"""

import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.config import RedisSettings
from app.models import Quote

logger = logging.getLogger(__name__)
_ACTIVE_KEY = "quant:quotes:active"
_LOCK_KEY = "quant:quotes:refresh-lock"
_SINGLE_QUOTE_KEY_PREFIX = "quant:quotes:single:"
_SINGLE_QUOTE_LOCK_PREFIX = "quant:quotes:single-refresh-lock:"
_RELEASE_LOCK_SCRIPT = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class CacheUnavailableError(RuntimeError):
    """实时行情缓存不可用。"""


class RedisCache:
    """封装版本化快照、过期时间和跨 worker 刷新锁。"""

    def __init__(self, settings: RedisSettings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client

    def _redis(self) -> Any:
        if self._client is None:
            try:
                import redis
            except ImportError as exc:
                raise CacheUnavailableError("缺少 redis-py 客户端，请按 requirements.txt 安装后重试") from exc
            self._client = redis.Redis(
                host=self._settings.host,
                port=self._settings.port,
                db=self._settings.db,
                password=self._settings.password or None,
                socket_timeout=self._settings.socket_timeout_seconds,
                decode_responses=True,
            )
        return self._client

    @staticmethod
    def _quote_fields(quote: Quote) -> dict[str, str]:
        """只写标量字段，避免把记录整体编码成 JSON 字符串。"""

        return {
            "symbol": quote.symbol,
            "name": quote.name,
            "latest_price": str(quote.latest_price),
            "change_amount": str(quote.change_amount),
            "change_percent": str(quote.change_percent),
            "open": str(quote.open),
            "high": str(quote.high),
            "low": str(quote.low),
            "previous_close": str(quote.previous_close),
            "volume_hands": str(quote.volume_hands),
            "turnover": str(quote.turnover),
            "source": quote.source,
        }

    def publish_snapshot(self, quotes: list[Quote], fetched_at: datetime) -> str:
        """分批写入版本 Hash，再原子更新活动版本指针。"""

        client = self._redis()
        generation = uuid4().hex
        base = f"quant:quotes:{generation}"
        symbols_key = f"{base}:symbols"
        ttl = self._settings.cache_ttl_seconds
        try:
            # 以小批 pipeline 降低往返次数，并避免单次请求积累过多命令。
            for offset in range(0, len(quotes), 500):
                pipeline = client.pipeline(transaction=False)
                for quote in quotes[offset : offset + 500]:
                    key = f"{base}:{quote.symbol}"
                    pipeline.hset(key, mapping=self._quote_fields(quote))
                    pipeline.expire(key, ttl)
                    pipeline.sadd(symbols_key, quote.symbol)
                pipeline.expire(symbols_key, ttl)
                pipeline.execute()

            metadata_key = f"{base}:meta"
            metadata = client.pipeline(transaction=False)
            metadata.hset(
                metadata_key,
                mapping={
                    "fetched_at": fetched_at.isoformat(),
                    "source": quotes[0].source if quotes else "AKShare",
                    "count": str(len(quotes)),
                },
            )
            metadata.expire(metadata_key, ttl)
            metadata.execute()
            # 指针最后发布，读者只会看到完整版本；失败时旧版本仍可用。
            client.set(_ACTIVE_KEY, generation, ex=ttl)
            return generation
        except Exception as exc:
            logger.exception("写入 Redis 行情快照失败")
            raise CacheUnavailableError("无法写入 Redis 行情缓存") from exc

    def publish_quote(self, quote: Quote) -> None:
        """以独立 Redis Hash 缓存单股报价，不改写全市场快照活动指针。"""

        client = self._redis()
        key = f"{_SINGLE_QUOTE_KEY_PREFIX}{quote.symbol}"
        fields = self._quote_fields(quote)
        fields["fetched_at"] = quote.fetched_at.isoformat()
        try:
            pipeline = client.pipeline(transaction=False)
            pipeline.hset(key, mapping=fields)
            pipeline.expire(key, self._settings.cache_ttl_seconds)
            pipeline.execute()
        except Exception as exc:
            logger.exception("写入 Redis 单股行情失败")
            raise CacheUnavailableError("无法写入 Redis 单股行情缓存") from exc

    def get_quote(self, symbol: str) -> Quote | None:
        client = self._redis()
        try:
            # 单股报价与全市场快照分开缓存，避免覆盖搜索页使用的整份快照。
            single_fields = client.hgetall(f"{_SINGLE_QUOTE_KEY_PREFIX}{symbol}")
            single_quote = self._quote_from_fields(single_fields, {}) if single_fields else None
            generation = client.get(_ACTIVE_KEY)
            snapshot_quote = None
            if generation:
                key = f"quant:quotes:{generation}:{symbol}"
                fields = client.hgetall(key)
                if fields:
                    metadata = client.hgetall(f"quant:quotes:{generation}:meta")
                    snapshot_quote = self._quote_from_fields(fields, metadata)
            if single_quote is None:
                return snapshot_quote
            if snapshot_quote is None or single_quote.fetched_at >= snapshot_quote.fetched_at:
                return single_quote
            return snapshot_quote
        except CacheUnavailableError:
            raise
        except Exception as exc:
            logger.exception("读取 Redis 实时行情失败")
            raise CacheUnavailableError("无法读取 Redis 行情缓存") from exc

    def get_quotes(self) -> list[Quote]:
        """按活动版本的股票索引批量读取 Hash，供代码/名称搜索复用。"""

        client = self._redis()
        try:
            generation = client.get(_ACTIVE_KEY)
            if not generation:
                return []
            base = f"quant:quotes:{generation}"
            symbols = sorted(client.smembers(f"{base}:symbols"))
            pipeline = client.pipeline(transaction=False)
            for symbol in symbols:
                pipeline.hgetall(f"{base}:{symbol}")
            rows = pipeline.execute() if symbols else []
            metadata = client.hgetall(f"{base}:meta")
            return [self._quote_from_fields(row, metadata) for row in rows if row]
        except CacheUnavailableError:
            raise
        except Exception as exc:
            logger.exception("批量读取 Redis 行情失败")
            raise CacheUnavailableError("无法读取 Redis 行情缓存") from exc

    def acquire_refresh_lock(self, token: str) -> bool:
        try:
            return bool(self._redis().set(_LOCK_KEY, token, nx=True, ex=self._settings.refresh_lock_seconds))
        except Exception as exc:
            logger.exception("获取 Redis 行情刷新锁失败")
            raise CacheUnavailableError("无法获取 Redis 行情刷新锁") from exc

    def release_refresh_lock(self, token: str) -> None:
        try:
            self._redis().eval(_RELEASE_LOCK_SCRIPT, 1, _LOCK_KEY, token)
        except Exception as exc:
            logger.exception("释放 Redis 行情刷新锁失败")
            raise CacheUnavailableError("无法释放 Redis 行情刷新锁") from exc

    def acquire_quote_refresh_lock(self, symbol: str, token: str) -> bool:
        """按股票代码加锁，避免单股刷新与全市场快照互相阻塞。"""

        try:
            key = f"{_SINGLE_QUOTE_LOCK_PREFIX}{symbol}"
            return bool(self._redis().set(key, token, nx=True, ex=self._settings.refresh_lock_seconds))
        except Exception as exc:
            logger.exception("获取 Redis 单股行情刷新锁失败")
            raise CacheUnavailableError("无法获取 Redis 单股行情刷新锁") from exc

    def release_quote_refresh_lock(self, symbol: str, token: str) -> None:
        try:
            key = f"{_SINGLE_QUOTE_LOCK_PREFIX}{symbol}"
            self._redis().eval(_RELEASE_LOCK_SCRIPT, 1, key, token)
        except Exception as exc:
            logger.exception("释放 Redis 单股行情刷新锁失败")
            raise CacheUnavailableError("无法释放 Redis 单股行情刷新锁") from exc

    @staticmethod
    def _quote_from_fields(fields: dict[str, str], metadata: dict[str, str]) -> Quote:
        fetched_at = datetime.fromisoformat(fields.get("fetched_at") or metadata["fetched_at"])
        return Quote(
            symbol=fields["symbol"],
            name=fields["name"],
            latest_price=float(fields["latest_price"]),
            change_amount=float(fields["change_amount"]),
            change_percent=float(fields["change_percent"]),
            open=float(fields["open"]),
            high=float(fields["high"]),
            low=float(fields["low"]),
            previous_close=float(fields["previous_close"]),
            volume_hands=float(fields["volume_hands"]),
            turnover=float(fields["turnover"]),
            source=fields.get("source", metadata.get("source", "AKShare")),
            fetched_at=fetched_at,
        )
