# tests/test_cache.py
"""
Coverage target: app/cache.py (Redis cache layer, FDD §11.1)

Covers:
- create_redis_pool() / get_redis_pool() / close_redis_pool() —
  success path, connection-failure-returns-None path, idempotent
  double-create, close-when-never-created
- Session helpers: set/get roundtrip, get on miss, delete, and every
  helper degrading gracefully (returns False/None, never raises) when
  Redis is unavailable or throws
- rate_limit_check(): under limit -> allowed, over limit -> blocked,
  Redis down -> fail-open (allowed), Redis exception mid-call ->
  fail-open, EXPIRE uses NX (checked via call args)
- Fleet-live snapshot cache: hit returns cached data without querying
  DB (verified at the app.cache level — routes_vehicles integration
  is a separate concern), miss returns None, write failure swallowed,
  read failure swallowed

Uses pytest-asyncio + unittest.mock (AsyncMock/MagicMock) — no real
Redis server required. Mirrors the mocking style already used for
asyncpg.Pool throughout this test suite (see test_trip_manager.py).
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "test_db")
os.environ.setdefault("DB_USER", "test_user")
os.environ.setdefault("DB_PASS", "test_pass")
os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("MQTT_PORT", "1883")
os.environ.setdefault("MQTT_TOPIC", "test/topic")

import pytest  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import app.cache as cache  # noqa: E402


# ── Fixture: reset the module-level shared pool between tests ──────
# cache._redis_pool is a module-global — without resetting, a mock
# client set up in one test would leak into the next test's assertions.
@pytest.fixture(autouse=True)
def _reset_cache_globals():
    cache._redis_pool = None
    yield
    cache._redis_pool = None


def _make_fake_redis_client():
    """Build an AsyncMock standing in for redis.asyncio.Redis."""
    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    client.set = AsyncMock(return_value=True)
    client.get = AsyncMock(return_value=None)
    client.delete = AsyncMock(return_value=1)
    client.aclose = AsyncMock(return_value=None)
    return client


def _make_fake_pipeline(incr_result: int):
    """
    pool.pipeline(transaction=True) -> async context manager whose
    .execute() returns [incr_result, expire_result] (matching the
    real redis-py pipeline().execute() return shape used by
    rate_limit_check()).
    """
    pipe = MagicMock()
    pipe.incr = MagicMock()
    pipe.expire = MagicMock()
    pipe.execute = AsyncMock(return_value=[incr_result, True])

    pipe_cm = MagicMock()
    pipe_cm.__aenter__ = AsyncMock(return_value=pipe)
    pipe_cm.__aexit__ = AsyncMock(return_value=False)
    return pipe_cm


# =================================================================
# create_redis_pool() / get_redis_pool() / close_redis_pool()
# =================================================================

async def test_create_redis_pool_success(monkeypatch):
    fake_client = _make_fake_redis_client()
    monkeypatch.setattr(cache.redis, "Redis", MagicMock(return_value=fake_client))

    pool = await cache.create_redis_pool()

    assert pool is fake_client
    fake_client.ping.assert_awaited_once()
    assert await cache.get_redis_pool() is fake_client


async def test_create_redis_pool_returns_none_on_connection_failure(monkeypatch):
    fake_client = _make_fake_redis_client()
    fake_client.ping = AsyncMock(side_effect=ConnectionError("refused"))
    monkeypatch.setattr(cache.redis, "Redis", MagicMock(return_value=fake_client))

    pool = await cache.create_redis_pool()

    assert pool is None
    assert await cache.get_redis_pool() is None


async def test_create_redis_pool_idempotent_does_not_recreate(monkeypatch):
    fake_client = _make_fake_redis_client()
    redis_ctor = MagicMock(return_value=fake_client)
    monkeypatch.setattr(cache.redis, "Redis", redis_ctor)

    first = await cache.create_redis_pool()
    second = await cache.create_redis_pool()

    assert first is second
    redis_ctor.assert_called_once()


async def test_close_redis_pool_when_never_created_is_noop():
    # should not raise even though _redis_pool is None
    await cache.close_redis_pool()
    assert await cache.get_redis_pool() is None


async def test_close_redis_pool_closes_and_clears_reference(monkeypatch):
    fake_client = _make_fake_redis_client()
    monkeypatch.setattr(cache.redis, "Redis", MagicMock(return_value=fake_client))
    await cache.create_redis_pool()

    await cache.close_redis_pool()

    fake_client.aclose.assert_awaited_once()
    assert await cache.get_redis_pool() is None


async def test_close_redis_pool_swallows_exception(monkeypatch):
    fake_client = _make_fake_redis_client()
    fake_client.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
    monkeypatch.setattr(cache.redis, "Redis", MagicMock(return_value=fake_client))
    await cache.create_redis_pool()

    # must not raise
    await cache.close_redis_pool()
    assert await cache.get_redis_pool() is None


# =================================================================
# Session helpers
# =================================================================

async def test_cache_set_and_get_session_roundtrip():
    fake_client = _make_fake_redis_client()
    cache._redis_pool = fake_client

    ok = await cache.cache_set_session("token-123", {"user_id": 55, "role": "admin"})
    assert ok is True
    fake_client.set.assert_awaited_once()
    _, kwargs = fake_client.set.call_args
    assert kwargs.get("ex") == cache.SESSION_TTL_SECONDS

    # simulate the stored JSON being returned on get
    import json
    stored_value = fake_client.set.call_args.args[1]
    fake_client.get = AsyncMock(return_value=stored_value)

    session = await cache.cache_get_session("token-123")
    assert session == json.loads(stored_value)


async def test_cache_get_session_miss_returns_none():
    fake_client = _make_fake_redis_client()
    fake_client.get = AsyncMock(return_value=None)
    cache._redis_pool = fake_client

    result = await cache.cache_get_session("nonexistent")
    assert result is None


async def test_cache_delete_session_success():
    fake_client = _make_fake_redis_client()
    cache._redis_pool = fake_client

    ok = await cache.cache_delete_session("token-123")
    assert ok is True
    fake_client.delete.assert_awaited_once()


async def test_session_helpers_degrade_gracefully_when_redis_none():
    cache._redis_pool = None

    assert await cache.cache_set_session("t", {"a": 1}) is False
    assert await cache.cache_get_session("t") is None
    assert await cache.cache_delete_session("t") is False


async def test_session_helpers_degrade_gracefully_on_redis_exception():
    fake_client = _make_fake_redis_client()
    fake_client.set = AsyncMock(side_effect=ConnectionError("down"))
    fake_client.get = AsyncMock(side_effect=ConnectionError("down"))
    fake_client.delete = AsyncMock(side_effect=ConnectionError("down"))
    cache._redis_pool = fake_client

    assert await cache.cache_set_session("t", {"a": 1}) is False
    assert await cache.cache_get_session("t") is None
    assert await cache.cache_delete_session("t") is False


async def test_cache_get_session_malformed_json_does_not_raise():
    fake_client = _make_fake_redis_client()
    fake_client.get = AsyncMock(return_value="{not valid json")
    cache._redis_pool = fake_client

    result = await cache.cache_get_session("t")
    assert result is None


# =================================================================
# rate_limit_check()
# =================================================================

async def test_rate_limit_check_allows_under_limit():
    fake_client = _make_fake_redis_client()
    fake_client.pipeline = MagicMock(return_value=_make_fake_pipeline(incr_result=3))
    cache._redis_pool = fake_client

    allowed = await cache.rate_limit_check("user:1", limit=10, window_seconds=60)
    assert allowed is True


async def test_rate_limit_check_blocks_over_limit():
    fake_client = _make_fake_redis_client()
    fake_client.pipeline = MagicMock(return_value=_make_fake_pipeline(incr_result=11))
    cache._redis_pool = fake_client

    allowed = await cache.rate_limit_check("user:1", limit=10, window_seconds=60)
    assert allowed is False


async def test_rate_limit_check_exactly_at_limit_is_allowed():
    fake_client = _make_fake_redis_client()
    fake_client.pipeline = MagicMock(return_value=_make_fake_pipeline(incr_result=10))
    cache._redis_pool = fake_client

    allowed = await cache.rate_limit_check("user:1", limit=10, window_seconds=60)
    assert allowed is True


async def test_rate_limit_check_uses_expire_nx_for_atomicity():
    fake_client = _make_fake_redis_client()
    pipe_cm = _make_fake_pipeline(incr_result=1)
    fake_client.pipeline = MagicMock(return_value=pipe_cm)
    cache._redis_pool = fake_client

    await cache.rate_limit_check("user:1", limit=10, window_seconds=30)

    pipe = await pipe_cm.__aenter__()
    pipe.expire.assert_called_once()
    _, expire_kwargs = pipe.expire.call_args
    assert expire_kwargs.get("nx") is True


async def test_rate_limit_check_fails_open_when_redis_none():
    cache._redis_pool = None

    allowed = await cache.rate_limit_check("user:1", limit=10, window_seconds=60)
    assert allowed is True


async def test_rate_limit_check_fails_open_on_redis_exception():
    fake_client = _make_fake_redis_client()
    fake_client.pipeline = MagicMock(side_effect=ConnectionError("down"))
    cache._redis_pool = fake_client

    allowed = await cache.rate_limit_check("user:1", limit=10, window_seconds=60)
    assert allowed is True


# =================================================================
# Fleet-live snapshot cache
# =================================================================

async def test_cache_fleet_live_snapshot_and_get_roundtrip():
    fake_client = _make_fake_redis_client()
    cache._redis_pool = fake_client

    payload = [{"vehicle_id": 101, "lat": 13.7, "lon": 100.5}]
    ok = await cache.cache_fleet_live_snapshot(payload)
    assert ok is True

    _, kwargs = fake_client.set.call_args
    assert kwargs.get("ex") == cache.FLEET_LIVE_TTL_SECONDS

    stored_value = fake_client.set.call_args.args[1]
    fake_client.get = AsyncMock(return_value=stored_value)

    result = await cache.get_cached_fleet_live_snapshot()
    assert result == payload


async def test_get_cached_fleet_live_snapshot_miss_returns_none():
    fake_client = _make_fake_redis_client()
    fake_client.get = AsyncMock(return_value=None)
    cache._redis_pool = fake_client

    result = await cache.get_cached_fleet_live_snapshot()
    assert result is None


async def test_fleet_live_cache_degrades_gracefully_when_redis_none():
    cache._redis_pool = None

    assert await cache.cache_fleet_live_snapshot([{"a": 1}]) is False
    assert await cache.get_cached_fleet_live_snapshot() is None


async def test_fleet_live_cache_degrades_gracefully_on_redis_exception():
    fake_client = _make_fake_redis_client()
    fake_client.set = AsyncMock(side_effect=ConnectionError("down"))
    fake_client.get = AsyncMock(side_effect=ConnectionError("down"))
    cache._redis_pool = fake_client

    assert await cache.cache_fleet_live_snapshot([{"a": 1}]) is False
    assert await cache.get_cached_fleet_live_snapshot() is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))
