# tests/api_test_routes_vehicles.py
"""
Coverage target: app/api/routes_vehicles.py

Endpoints covered:
  - GET /api/v1/vehicles                       : list all vehicles
  - GET /api/v1/vehicles/{vehicle_id}/device    : device binding info
  - GET /api/v1/vehicles/{vehicle_id}/location  : latest GPS/telemetry
  - GET /api/v1/vehicles/{vehicle_id}/trips     : paginated trip history
  - GET /api/v1/fleet/live                      : SSE stream

Testing strategy
-----------------
Like routes_drivers.py, most endpoints call `get_db_connection()` directly
(wrapping asyncpg.connect()) rather than using FastAPI's Depends(). We
monkeypatch `routes_vehicles.get_db_connection` with an AsyncMock. All
non-SSE endpoints require the `APIKEY` header via Security(verify_api_key).

[FIX HISTORY]

Round 1 (RecursionError):
  `asyncio` is a singleton module — `routes_vehicles.asyncio` IS the same
  object as the real `asyncio` module. Patching `routes_vehicles.asyncio.sleep`
  therefore patches the real `asyncio.sleep` globally. The old `_fast_sleep`
  looked up `asyncio.sleep` fresh on every call, so once patched it ended up
  calling itself forever -> RecursionError.

Round 2 (Timeout, this fix):
  Starlette's TestClient runs the ASGI app to completion in a background
  thread BEFORE handing any response back to the test — even when called
  via `client.stream(...)`. The `/api/v1/fleet/live` endpoint is a genuine
  `while True:` infinite SSE generator by design, so `app()` never returns
  on its own -> the previous "just make sleep resolve fast" patch caused
  the loop to spin forever, timing out at pytest-timeout's limit.

  Fix: make the patched `asyncio.sleep` raise `asyncio.CancelledError`
  immediately after the first fetch+yield cycle. This exactly matches the
  "client disconnected" code path already handled in
  routes_vehicles.py's `event_generator()`:

      try:
          while True:
              ...
              yield f"data: {data}\n\n"
              await asyncio.sleep(5)      # <- patched to raise CancelledError here
      except asyncio.CancelledError:
          logger.info("Client disconnected — stream closed")
          return                          # <- generator finishes -> app() returns

  With this, exactly one real SSE chunk is produced, then the ASGI call
  completes normally instead of hanging.
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
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api import routes_vehicles  # noqa: E402
from app.database import get_db_pool  # noqa: E402

VALID_KEY = "ktc-fleet-2026-secret"


# =================================================================
# Fixtures — REST endpoints (raw asyncpg.connect() pattern)
# =================================================================

def _make_conn(fetch_return=None, fetchrow_return=None, fetchval_return=0):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.close = AsyncMock(return_value=None)
    return conn


@pytest.fixture
def conn():
    return _make_conn()


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setattr(
        routes_vehicles, "get_db_connection", AsyncMock(return_value=conn)
    )
    app = FastAPI()
    app.include_router(routes_vehicles.router)
    app.include_router(routes_vehicles.fleet_router)
    return TestClient(app, headers={"APIKEY": VALID_KEY})


# =================================================================
# Fixtures — SSE endpoint (shared pool via Depends(get_db_pool))
# =================================================================

@pytest.fixture
def sse_pool():
    """Mock pool whose .fetch() resolves instantly with an empty list."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    return pool


@pytest.fixture
def sse_client(sse_pool, monkeypatch):
    # [FIX — Timeout] TestClient runs the ASGI app to completion before
    # returning anything, so a genuinely infinite `while True` SSE generator
    # will hang the test forever. We patch `asyncio.sleep` (called once per
    # loop iteration inside event_generator()) to raise CancelledError right
    # after the first chunk is yielded — mirroring "client disconnected",
    # which the endpoint already handles gracefully by returning from the
    # generator. This lets exactly one chunk be produced and the request
    # complete deterministically instead of spinning until pytest-timeout
    # kills it.
    import asyncio as _asyncio

    async def _stop_after_first_chunk(seconds):
        raise _asyncio.CancelledError()

    monkeypatch.setattr(routes_vehicles.asyncio, "sleep", _stop_after_first_chunk)

    app = FastAPI()
    app.include_router(routes_vehicles.fleet_router)

    async def _override_get_db_pool():
        return sse_pool

    app.dependency_overrides[get_db_pool] = _override_get_db_pool
    return TestClient(app, headers={"APIKEY": VALID_KEY})


# =================================================================
# Auth
# =================================================================

def test_get_all_vehicles_rejects_missing_key(conn, monkeypatch):
    monkeypatch.setattr(
        routes_vehicles, "get_db_connection", AsyncMock(return_value=conn)
    )
    app = FastAPI()
    app.include_router(routes_vehicles.router)
    client = TestClient(app)  # no header

    resp = client.get("/api/v1/vehicles")

    assert resp.status_code == 403


def test_get_all_vehicles_rejects_wrong_key(conn, monkeypatch):
    monkeypatch.setattr(
        routes_vehicles, "get_db_connection", AsyncMock(return_value=conn)
    )
    app = FastAPI()
    app.include_router(routes_vehicles.router)
    client = TestClient(app, headers={"APIKEY": "nope"})

    resp = client.get("/api/v1/vehicles")

    assert resp.status_code == 403


# =================================================================
# GET /api/v1/vehicles
# =================================================================

def test_get_all_vehicles_returns_list(client, conn):
    conn.fetch = AsyncMock(return_value=[
        {"vehicle_id": 101, "device_id": "KTC-001", "driver_id": 55,
         "date_update_latest": None, "active": True,
         "lat": 13.7, "lon": 100.5, "speed": 40.0, "ignition": True, "last_seen": None},
    ])

    resp = client.get("/api/v1/vehicles")

    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_all_vehicles_empty(client, conn):
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/vehicles")

    assert resp.status_code == 200
    assert resp.json() == []


def test_get_all_vehicles_db_error_returns_500(client, conn):
    conn.fetch = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.get("/api/v1/vehicles")

    assert resp.status_code == 500


# =================================================================
# GET /api/v1/vehicles/{vehicle_id}/device
# =================================================================

def test_get_vehicle_device_found(client, conn):
    conn.fetchrow = AsyncMock(return_value={
        "vehicle_id": 101, "device_id": "KTC-001", "driver_id": 55,
        "active": True, "firmware_ver": "1.0.0",
        "date_update_latest": None, "has_telemetry": True,
    })

    resp = client.get("/api/v1/vehicles/101/device")

    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "KTC-001"
    assert body["has_telemetry"] is True


def test_get_vehicle_device_not_found_returns_404(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/vehicles/999/device")

    assert resp.status_code == 404


def test_get_vehicle_device_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("boom"))

    resp = client.get("/api/v1/vehicles/101/device")

    assert resp.status_code == 500


# =================================================================
# GET /api/v1/vehicles/{vehicle_id}/location
# =================================================================

def test_get_vehicle_location_success(client, conn):
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": "KTC-001"},  # active device lookup
        {"ts": "2026-06-01T10:00:00Z", "lat": 13.7, "lon": 100.5,
         "speed": 45.0, "heading": 90, "ignition": True, "event": None},
    ])

    resp = client.get("/api/v1/vehicles/101/location")

    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "KTC-001"
    assert body["event"] is None


def test_get_vehicle_location_no_active_device_returns_404(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/vehicles/101/location")

    assert resp.status_code == 404


def test_get_vehicle_location_no_telemetry_yet_returns_404(client, conn):
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": "KTC-001"},  # device found
        None,               # but no telemetry rows yet
    ])

    resp = client.get("/api/v1/vehicles/101/location")

    assert resp.status_code == 404


def test_get_vehicle_location_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db exploded"))

    resp = client.get("/api/v1/vehicles/101/location")

    assert resp.status_code == 500


# =================================================================
# GET /api/v1/vehicles/{vehicle_id}/trips
# =================================================================

def test_get_vehicle_trips_default_pagination(client, conn):
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[
        {"id": 1, "device_id": "KTC-001", "vehicle_id": 101, "driver_id": 55,
         "trip_start": None, "trip_end": None, "distance_km": 10.0,
         "duration_min": 20.0, "idle_min": 1.0, "max_speed": 80.0,
         "avg_speed": 40.0, "harsh_brake_count": 0, "harsh_accel_count": 0,
         "harsh_corner_count": 0, "speeding_count": 0, "driver_score": 90.0,
         "fuel_used": 1.0, "synced_to_odoo": True, "synced_at": None,
         "created_at": None},
    ])

    resp = client.get("/api/v1/vehicles/101/trips")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1


def test_get_vehicle_trips_with_date_and_synced_filters(client, conn):
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get(
        "/api/v1/vehicles/101/trips"
        "?date_from=2026-01-01T00:00:00&date_to=2026-06-30T23:59:59"
        "&synced_only=true&page=2&limit=10"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["filters"]["synced_only"] is True
    assert body["page"] == 2


def test_get_vehicle_trips_limit_over_max_rejected(client):
    resp = client.get("/api/v1/vehicles/101/trips?limit=500")
    assert resp.status_code == 422  # le=200 constraint


def test_get_vehicle_trips_db_error_returns_500(client, conn):
    conn.fetchval = AsyncMock(side_effect=RuntimeError("count failed"))

    resp = client.get("/api/v1/vehicles/101/trips")

    assert resp.status_code == 500


# =================================================================
# GET /api/v1/fleet/live (SSE)
# =================================================================

def test_fleet_live_rejects_missing_key():
    app = FastAPI()
    app.include_router(routes_vehicles.fleet_router)
    client = TestClient(app)

    resp = client.get("/api/v1/fleet/live")

    assert resp.status_code == 403


@pytest.mark.timeout(10)  # safety net — should return almost instantly now
def test_fleet_live_accepts_valid_key_and_streams(sse_client, sse_pool):
    """
    Uses the `sse_client` fixture (mocked pool + a patched asyncio.sleep
    that raises CancelledError after the first chunk). The stream yields
    its one chunk, the generator returns cleanly, and the ASGI call
    completes — no more hanging until pytest-timeout intervenes.
    """
    with sse_client.stream("GET", "/api/v1/fleet/live") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        chunk_iter = resp.iter_lines()
        first_line = next(chunk_iter)
        assert first_line.startswith("data:")

    sse_pool.fetch.assert_awaited()


def test_fleet_live_wrong_key_rejected_before_pool_used(sse_client, sse_pool, monkeypatch):
    monkeypatch.setattr(
        sse_client, "headers", {**sse_client.headers, "APIKEY": "wrong-key"}
    )

    resp = sse_client.get("/api/v1/fleet/live", headers={"APIKEY": "wrong-key"})

    assert resp.status_code == 403
    sse_pool.fetch.assert_not_awaited()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))