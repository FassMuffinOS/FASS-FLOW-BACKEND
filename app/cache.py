"""Upstash Redis caching layer."""
import json
from typing import Any
from upstash_redis import Redis
from app.config import settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis(
            url=settings.upstash_redis_rest_url,
            token=settings.upstash_redis_rest_token,
        )
    return _redis


async def cache_get(key: str) -> Any | None:
    r = get_redis()
    val = r.get(key)
    if val is None:
        return None
    return json.loads(val)


async def cache_set(key: str, value: Any, ex: int = 300) -> None:
    r = get_redis()
    r.set(key, json.dumps(value), ex=ex)


async def cache_delete(key: str) -> None:
    r = get_redis()
    r.delete(key)
