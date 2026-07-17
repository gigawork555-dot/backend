# tests/api_test_routes_vehicles.py
"""
Coverage target: app/api/routes_vehicles.py
... (docstring เดิมคงไว้) ...
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

# [เพิ่มใหม่] ต้อง insert _TEST_DIR ด้วย เพื่อ import conftest ได้
# (ไฟล์เดิมมีแค่ _REPO_ROOT ทำให้ "from conftest import ..." หา module ไม่เจอ
# ถ้ารันจาก working directory อื่นที่ไม่ใช่ tests/ โดยตรง)
_TEST_DIR = os.path.dirname(__file__)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from conftest import check, check_is, check_approx  # noqa: E402

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
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    return pool


@pytest.fixture
def sse_client(sse_pool, monkeypatch):
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

    check("resp.status_code (no key)", resp.status_code, 403)


def test_get_all_vehicles_rejects_wrong_key(conn, monkeypatch):
    monkeypatch.setattr(
        routes_vehicles, "get_db_connection", AsyncMock(return_value=conn)
    )
    app = FastAPI()
    app.include_router(routes_vehicles.router)
    client = TestClient(app, headers={"APIKEY": "nope"})

    resp = client.get("/api/v1/vehicles")

    check("resp.status_code (wrong key)", resp.status_code, 403)


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

    check("resp.status_code", resp.status_code, 200)
    check("len(resp.json())", len(resp.json()), 1)


def test_get_all_vehicles_empty(client, conn):
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/vehicles")

    check("resp.status_code", resp.status_code, 200)
    check("resp.json()", resp.json(), [])


def test_get_all_vehicles_db_error_returns_500(client, conn):
    conn.fetch = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.get("/api/v1/vehicles")

    check("resp.status_code (db error)", resp.status_code, 500)


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

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['device_id']", body["device_id"], "KTC-001")
    check_is("body['has_telemetry']", body["has_telemetry"], True)


def test_get_vehicle_device_not_found_returns_404(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/vehicles/999/device")

    check("resp.status_code (not found)", resp.status_code, 404)


def test_get_vehicle_device_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("boom"))

    resp = client.get("/api/v1/vehicles/101/device")

    check("resp.status_code (db error)", resp.status_code, 500)


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

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['device_id']", body["device_id"], "KTC-001")
    check_is("body['event']", body["event"], None)


def test_get_vehicle_location_no_active_device_returns_404(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/vehicles/101/location")

    check("resp.status_code (no active device)", resp.status_code, 404)


def test_get_vehicle_location_no_telemetry_yet_returns_404(client, conn):
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": "KTC-001"},  # device found
        None,               # but no telemetry rows yet
    ])

    resp = client.get("/api/v1/vehicles/101/location")

    check("resp.status_code (no telemetry yet)", resp.status_code, 404)


def test_get_vehicle_location_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db exploded"))

    resp = client.get("/api/v1/vehicles/101/location")

    check("resp.status_code (db error)", resp.status_code, 500)


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

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['total']", body["total"], 1)
    check("body['page']", body["page"], 1)


def test_get_vehicle_trips_with_date_and_synced_filters(client, conn):
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get(
        "/api/v1/vehicles/101/trips"
        "?date_from=2026-01-01T00:00:00&date_to=2026-06-30T23:59:59"
        "&synced_only=true&page=2&limit=10"
    )

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check_is("body['filters']['synced_only']", body["filters"]["synced_only"], True)
    check("body['page']", body["page"], 2)


def test_get_vehicle_trips_limit_over_max_rejected(client):
    resp = client.get("/api/v1/vehicles/101/trips?limit=500")
    check("resp.status_code (limit>200)", resp.status_code, 422)


def test_get_vehicle_trips_db_error_returns_500(client, conn):
    conn.fetchval = AsyncMock(side_effect=RuntimeError("count failed"))

    resp = client.get("/api/v1/vehicles/101/trips")

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# GET /api/v1/fleet/live (SSE)
# =================================================================

def test_fleet_live_rejects_missing_key():
    app = FastAPI()
    app.include_router(routes_vehicles.fleet_router)
    client = TestClient(app)

    resp = client.get("/api/v1/fleet/live")

    check("resp.status_code (no key)", resp.status_code, 403)


@pytest.mark.timeout(10)
def test_fleet_live_accepts_valid_key_and_streams(sse_client, sse_pool):
    with sse_client.stream("GET", "/api/v1/fleet/live") as resp:
        check("resp.status_code", resp.status_code, 200)
        content_type = resp.headers["content-type"]
        print(f"  🔎 {'content-type contains text/event-stream':<28} -> actual={content_type!r}")
        assert "text/event-stream" in content_type

        chunk_iter = resp.iter_lines()
        first_line = next(chunk_iter)
        print(f"  🔎 {'first_line startswith data:':<28} -> actual={first_line!r}")
        assert first_line.startswith("data:")

    sse_pool.fetch.assert_awaited()
    print("  🔎 sse_pool.fetch awaited        -> actual=True expected=True ✅")


def test_fleet_live_wrong_key_rejected_before_pool_used(sse_client, sse_pool, monkeypatch):
    monkeypatch.setattr(
        sse_client, "headers", {**sse_client.headers, "APIKEY": "wrong-key"}
    )

    resp = sse_client.get("/api/v1/fleet/live", headers={"APIKEY": "wrong-key"})

    check("resp.status_code (wrong key)", resp.status_code, 403)
    sse_pool.fetch.assert_not_awaited()
    print("  🔎 sse_pool.fetch NOT awaited    -> actual=True expected=True ✅")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))