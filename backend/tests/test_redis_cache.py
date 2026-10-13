import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cache.redis_cache import RedisCache
from app.config import RedisSettings
from app.models import Quote


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.commands: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str):
        def queue(*args: object, **kwargs: object):
            self.commands.append((name, args, kwargs))
            return self
        return queue

    def execute(self) -> list[object]:
        results = []
        for name, args, kwargs in self.commands:
            results.append(getattr(self.redis, name)(*args, **kwargs))
        self.commands.clear()
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self, transaction: bool = False) -> FakePipeline:
        return FakePipeline(self)

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    def hgetall(self, key: str) -> dict[str, str]:
        return self.hashes.get(key, {}).copy()

    def sadd(self, key: str, value: str) -> None:
        self.sets.setdefault(key, set()).add(value)

    def smembers(self, key: str) -> set[str]:
        return self.sets.get(key, set()).copy()

    def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def eval(self, script: str, count: int, key: str, token: str) -> int:
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


def quote(symbol: str = "600000") -> Quote:
    return Quote(
        symbol=symbol, name="浦发银行", latest_price=8.2, change_amount=0.1,
        change_percent=1.23, open=8.1, high=8.3, low=8.0, previous_close=8.1,
        volume_hands=100, turnover=820000, source="AKShare 东方财富",
        fetched_at=datetime(2024, 1, 2, tzinfo=timezone.utc), stale=False,
    )


def redis_settings() -> RedisSettings:
    return RedisSettings("127.0.0.1", 6379, 0, "", 3, 300, 60, 180)


class RedisCacheTests(unittest.TestCase):
    def test_publishes_structured_hash_before_switching_active_snapshot(self) -> None:
        redis = FakeRedis()
        cache = RedisCache(redis_settings(), client=redis)

        generation = cache.publish_snapshot([quote()], quote().fetched_at)

        self.assertEqual(redis.get("quant:quotes:active"), generation)
        fields = redis.hgetall(f"quant:quotes:{generation}:600000")
        self.assertEqual(fields["latest_price"], "8.2")
        self.assertNotIn("{", fields["latest_price"])
        self.assertEqual(redis.ttls[f"quant:quotes:{generation}:600000"], 300)
        self.assertEqual(cache.get_quote("600000").name, "浦发银行")

    def test_snapshot_search_reads_each_symbol_hash(self) -> None:
        redis = FakeRedis()
        cache = RedisCache(redis_settings(), client=redis)
        cache.publish_snapshot([quote("600000"), quote("000001")], quote().fetched_at)

        quotes = cache.get_quotes()

        self.assertEqual({item.symbol for item in quotes}, {"600000", "000001"})

    def test_single_quote_is_cached_as_scalar_hash_without_replacing_snapshot(self) -> None:
        redis = FakeRedis()
        cache = RedisCache(redis_settings(), client=redis)
        generation = cache.publish_snapshot([quote("000001")], quote().fetched_at)
        current = quote("600000")

        cache.publish_quote(current)

        fields = redis.hgetall("quant:quotes:single:600000")
        self.assertEqual(fields["latest_price"], "8.2")
        self.assertEqual(redis.ttls["quant:quotes:single:600000"], 300)
        self.assertEqual(redis.get("quant:quotes:active"), generation)
        self.assertEqual(cache.get_quote("600000").name, "浦发银行")
        self.assertEqual({item.symbol for item in cache.get_quotes()}, {"000001"})

    def test_refresh_lock_is_exclusive_and_release_checks_owner(self) -> None:
        redis = FakeRedis()
        cache = RedisCache(redis_settings(), client=redis)

        self.assertTrue(cache.acquire_refresh_lock("first"))
        self.assertFalse(cache.acquire_refresh_lock("second"))
        cache.release_refresh_lock("second")
        self.assertEqual(redis.get("quant:quotes:refresh-lock"), "first")
        cache.release_refresh_lock("first")
        self.assertIsNone(redis.get("quant:quotes:refresh-lock"))


if __name__ == "__main__":
    unittest.main()
