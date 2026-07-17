# tests/api_test_routes_trips.py
"""
Coverage target: app/api/routes_trips.py (FDD §11.3)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

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

_TEST_DIR = os.path.dirname(__file__)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from conftest import check, check_is, check_approx  # noqa: E402

from app.api import routes_trips     # noqa: E402
from app.database import get_db_pool  # noqa: E402


# =================================================================
# Fixtures
# =================================================================

def _make_tx_cm():
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    return tx_cm


def _make_conn():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_make_tx_cm())
    return conn


@pytest.fixture
def conn():
    return _make_conn()


@pytest.fixture
def pool(conn):
    pool = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)

    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=0)
    pool.execute = AsyncMock(return_value="UPDATE 1")
    return pool


@pytest.fixture
def client(pool):
    app = FastAPI()
    app.include_router(routes_trips.router)

    async def _override():
        return pool

    app.dependency_overrides[get_db_pool] = _override
    return TestClient(app)


def _trip_row(**overrides):
    base = {
        "id": 1, "device_id": "KTC-001", "vehicle_id": 101, "driver_id": 55,
        "trip_start": datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
        "trip_end": datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc),
        "distance_km": 12.5, "duration_min": 30.0, "idle_min": 2.0,
        "max_speed": 80.0, "avg_speed": 45.0,
        "harsh_brake_count": 0, "harsh_accel_count": 0,
        "harsh_corner_count": 0, "speeding_count": 0,
        "driver_score": 92.0, "fuel_used": 1.2,
        "gps_track": [{"lat": 13.7, "lon": 100.5}],
        "synced_to_odoo": False, "synced_at": None,
        "created_at": datetime(2026, 6, 1, 8, 31, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


# =================================================================
# POST /api/v1/webhook/odoo-sync
# =================================================================

def test_odoo_sync_webhook_returns_unsynced_batch(client, pool):
    pool.fetch = AsyncMock(return_value=[_trip_row()])

    resp = client.post("/api/v1/webhook/odoo-sync", json={})

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['total']", body["total"], 1)
    print(f"  🔎 {'last_sync_timestamp in body':<28} -> actual={'last_sync_timestamp' in body}")
    assert "last_sync_timestamp" in body
    check("len(body['trips'])", len(body["trips"]), 1)


def test_odoo_sync_webhook_with_cursor_filters_created_at(client, pool):
    pool.fetch = AsyncMock(return_value=[])

    resp = client.post(
        "/api/v1/webhook/odoo-sync",
        json={"last_sync_timestamp": "2026-06-01T00:00:00Z"},
    )

    check("resp.status_code", resp.status_code, 200)
    check("body['total']", resp.json()["total"], 0)
    _, call_args, _ = pool.fetch.mock_calls[0]
    print(f"  🔎 {'SQL contains created_at >':<28} -> actual={'created_at >' in call_args[0]}")
    assert "created_at >" in call_args[0]


def test_odoo_sync_webhook_empty_result(client, pool):
    pool.fetch = AsyncMock(return_value=[])

    resp = client.post("/api/v1/webhook/odoo-sync", json={})

    check("resp.status_code", resp.status_code, 200)
    check("body['trips']", resp.json()["trips"], [])


def test_odoo_sync_webhook_db_error_returns_500(client, pool):
    pool.fetch = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.post("/api/v1/webhook/odoo-sync", json={})

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# GET /api/v1/trips/unsynced
# =================================================================

def test_get_unsynced_trips_no_filters(client, pool):
    pool.fetch = AsyncMock(return_value=[_trip_row(id=7)])

    resp = client.get("/api/v1/trips/unsynced")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['total']", body["total"], 1)
    check("body['last_id']", body["last_id"], 7)


def test_get_unsynced_trips_with_all_filters(client, pool):
    pool.fetch = AsyncMock(return_value=[])

    resp = client.get(
        "/api/v1/trips/unsynced"
        "?vehicle_id=101&device_id=KTC-001&driver_id=55"
        "&since=2026-06-01T00:00:00Z&last_id=3&limit=50"
    )

    check("resp.status_code", resp.status_code, 200)
    check("body['total']", resp.json()["total"], 0)


def test_get_unsynced_trips_empty_returns_null_last_id(client, pool):
    pool.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/trips/unsynced")

    check_is("body['last_id']", resp.json()["last_id"], None)


def test_get_unsynced_trips_db_error_returns_500(client, pool):
    pool.fetch = AsyncMock(side_effect=RuntimeError("boom"))

    resp = client.get("/api/v1/trips/unsynced")

    check("resp.status_code (db error)", resp.status_code, 500)


def test_unsynced_route_registered_before_trip_id_route(client, pool):
    pool.fetch = AsyncMock(return_value=[])
    resp = client.get("/api/v1/trips/unsynced")
    check("resp.status_code (route precedence)", resp.status_code, 200)


# =================================================================
# PATCH /api/v1/trips/batch/mark-synced
# =================================================================

def test_batch_mark_synced_success(client, pool):
    resp = client.patch(
        "/api/v1/trips/batch/mark-synced",
        json={"trip_ids": [1, 2, 3]},
    )

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['status']", body["status"], "success")
    check("body['marked']", body["marked"], 3)


def test_batch_mark_synced_empty_list_returns_400(client):
    resp = client.patch("/api/v1/trips/batch/mark-synced", json={"trip_ids": []})
    check("resp.status_code (empty list)", resp.status_code, 400)


def test_batch_mark_synced_db_error_returns_500(client, conn):
    conn.execute = AsyncMock(side_effect=RuntimeError("tx failed"))

    resp = client.patch(
        "/api/v1/trips/batch/mark-synced", json={"trip_ids": [1]}
    )

    check("resp.status_code (db error)", resp.status_code, 500)


def test_batch_route_registered_before_trip_id_route(client):
    resp = client.patch("/api/v1/trips/batch/mark-synced", json={"trip_ids": [9]})
    check("resp.status_code (route precedence)", resp.status_code, 200)


# =================================================================
# PATCH /api/v1/trips/{trip_id}/mark-synced
# =================================================================

def test_mark_trip_synced_success(client, pool):
    pool.fetchrow = AsyncMock(side_effect=[
        {"id": 10, "synced_to_odoo": False, "synced_at": None},
        {"id": 10, "synced_to_odoo": True,
         "synced_at": datetime(2026, 6, 1, tzinfo=timezone.utc)},
    ])

    resp = client.patch("/api/v1/trips/10/mark-synced")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['status']", body["status"], "success")
    check_is("body['synced_to_odoo']", body["synced_to_odoo"], True)


def test_mark_trip_synced_idempotent_already_synced(client, pool):
    pool.fetchrow = AsyncMock(return_value={
        "id": 10, "synced_to_odoo": True,
        "synced_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
    })

    resp = client.patch("/api/v1/trips/10/mark-synced")

    check("resp.status_code", resp.status_code, 200)
    check("body['status']", resp.json()["status"], "already_synced")


def test_mark_trip_synced_not_found_returns_404(client, pool):
    pool.fetchrow = AsyncMock(return_value=None)

    resp = client.patch("/api/v1/trips/999/mark-synced")

    check("resp.status_code (not found)", resp.status_code, 404)


def test_mark_trip_synced_db_error_returns_500(client, pool):
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db exploded"))

    resp = client.patch("/api/v1/trips/10/mark-synced")

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# GET /api/v1/trips/{trip_id}
# =================================================================

def test_get_trip_detail_returns_full_record_with_events(client, pool):
    pool.fetchrow = AsyncMock(return_value=_trip_row(driver_score=92.0))
    pool.fetch = AsyncMock(return_value=[
        {"ts": "2026-06-01T08:10:00Z", "lat": 13.7, "lon": 100.5, "speed": 90.0,
         "event": "speeding", "event_severity": 0.8, "ax": 0.0, "ay": 0.0, "az": 1.0},
    ])

    resp = client.get("/api/v1/trips/1")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['event_count']", body["event_count"], 1)
    check("body['incentive_tier']", body["incentive_tier"], "A")


def test_get_trip_detail_not_found_returns_404(client, pool):
    pool.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/trips/9999")

    check("resp.status_code (not found)", resp.status_code, 404)


def test_get_trip_detail_exclude_gps_track_when_requested(client, pool):
    pool.fetchrow = AsyncMock(return_value=_trip_row())
    pool.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/trips/1?include_gps_track=false")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    print(f"  🔎 {'gps_track not in body':<28} -> actual={'gps_track' not in body}")
    assert "gps_track" not in body


def test_get_trip_detail_tier_boundaries(client, pool):
    for score, expected_tier in [(95.0, "A"), (80.0, "B"), (65.0, "C"), (30.0, "D")]:
        pool.fetchrow = AsyncMock(return_value=_trip_row(driver_score=score))
        pool.fetch = AsyncMock(return_value=[])

        resp = client.get("/api/v1/trips/1")
        check(f"tier @ score={score}", resp.json()["incentive_tier"], expected_tier)


def test_get_trip_detail_db_error_returns_500(client, pool):
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("query error"))

    resp = client.get("/api/v1/trips/1")

    check("resp.status_code (db error)", resp.status_code, 500)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))