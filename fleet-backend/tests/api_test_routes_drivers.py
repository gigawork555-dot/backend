# tests/test_routes_drivers.py
"""
Coverage target: app/api/routes_drivers.py

Endpoints covered:
  - GET /api/v1/drivers/{driver_id}/bonus         : FDD §12.4 incentive summary
  - GET /api/v1/drivers/{driver_id}/score          : avg score + tier + monthly trend
  - GET /api/v1/drivers/{driver_id}/events         : FDD §12.6 event history (pagination/filter)
  - GET /api/v1/drivers/{driver_id}/fuel-summary   : FDD §2.1 fuel/idling summary

Testing strategy
-----------------
routes_drivers.py does NOT use FastAPI's `Depends(get_db_pool)` — every
endpoint calls the module-level `get_db_connection()` helper directly,
which wraps `asyncpg.connect()`. There is no dependency to override via
`app.dependency_overrides`, so instead we monkeypatch the module
attribute `routes_drivers.get_db_connection` with an AsyncMock that
returns our fake connection. Because Python resolves `get_db_connection`
at call time (not import time) inside each endpoint function, this
monkeypatch is picked up correctly.

Every endpoint also requires the `APIKEY` header (Security(_verify_api_key)).
The `client` fixture bakes the correct key into every request by default;
individual tests override headers to exercise the 403 path.
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

from app.api import routes_drivers  # noqa: E402

VALID_KEY = "ktc-fleet-2026-secret"


# =================================================================
# Fixtures
# =================================================================

def _make_conn(fetch_return=None, fetchrow_return=None, fetch_side_effect=None):
    conn = MagicMock()
    if fetch_side_effect is not None:
        conn.fetch = AsyncMock(side_effect=fetch_side_effect)
    else:
        conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=0)
    conn.close = AsyncMock(return_value=None)
    return conn


@pytest.fixture
def conn():
    return _make_conn()


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setattr(
        routes_drivers, "get_db_connection", AsyncMock(return_value=conn)
    )
    app = FastAPI()
    app.include_router(routes_drivers.router)
    return TestClient(app, headers={"APIKEY": VALID_KEY})


# =================================================================
# Auth
# =================================================================

def test_bonus_rejects_missing_api_key():
    app = FastAPI()
    app.include_router(routes_drivers.router)
    client = TestClient(app)  # no APIKEY header at all
    resp = client.get("/api/v1/drivers/55/bonus")
    assert resp.status_code == 403


def test_bonus_rejects_wrong_api_key(conn, monkeypatch):
    monkeypatch.setattr(
        routes_drivers, "get_db_connection", AsyncMock(return_value=conn)
    )
    app = FastAPI()
    app.include_router(routes_drivers.router)
    client = TestClient(app, headers={"APIKEY": "wrong-key"})
    resp = client.get("/api/v1/drivers/55/bonus")
    assert resp.status_code == 403


# =================================================================
# GET /{driver_id}/bonus
# =================================================================

def test_bonus_no_trips_returns_zero_defaults(client, conn):
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)  # no active config -> fallback

    resp = client.get("/api/v1/drivers/55/bonus?month=6&year=2026")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_trips"] == 0
    assert body["avg_score"] is None
    assert body["incentive_tier"] == "D"
    assert body["bonus_pct"] == 0.0
    assert body["scoring_config_snapshot"]["score_base"] == 100.0


def test_bonus_computes_avg_and_tier_a(client, conn):
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 1, "driver_score": 95.0, "distance_km": 10.0,
            "harsh_brake_count": 0, "harsh_accel_count": 0,
            "harsh_corner_count": 0, "speeding_count": 0, "idle_min": 2.0,
        },
        {
            "id": 2, "driver_score": 91.0, "distance_km": 8.0,
            "harsh_brake_count": 1, "harsh_accel_count": 0,
            "harsh_corner_count": 0, "speeding_count": 0, "idle_min": 1.0,
        },
    ])
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/drivers/55/bonus")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_trips"] == 2
    assert body["avg_score"] == 93.0
    assert body["incentive_tier"] == "A"
    assert body["bonus_pct"] == 10.0
    assert body["total_harsh_events"] == 1


def test_bonus_tier_c_gives_zero_pct_per_fdd(client, conn):
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 1, "driver_score": 65.0, "distance_km": 5.0,
            "harsh_brake_count": 0, "harsh_accel_count": 0,
            "harsh_corner_count": 0, "speeding_count": 0, "idle_min": 0.0,
        },
    ])
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/drivers/55/bonus")

    body = resp.json()
    assert body["incentive_tier"] == "C"
    assert body["bonus_pct"] == 0.0  # [FIX-3] not 2%


def test_bonus_custom_tier_thresholds_from_query_params(client, conn):
    conn.fetch = AsyncMock(return_value=[
        {
            "id": 1, "driver_score": 80.0, "distance_km": 5.0,
            "harsh_brake_count": 0, "harsh_accel_count": 0,
            "harsh_corner_count": 0, "speeding_count": 0, "idle_min": 0.0,
        },
    ])
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get(
        "/api/v1/drivers/55/bonus?tier_a_min=70&tier_b_min=50&tier_c_min=30"
    )

    body = resp.json()
    # score 80 >= custom tier_a_min (70) -> Tier A under these thresholds
    assert body["incentive_tier"] == "A"
    assert body["tier_thresholds"]["tier_a_min"] == 70.0


def test_bonus_non_digit_driver_id_treated_as_zero(client, conn):
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/drivers/abc/bonus")

    assert resp.status_code == 200
    assert resp.json()["driver_id"] == "abc"


def test_bonus_db_error_returns_500(client, conn, monkeypatch):
    monkeypatch.setattr(
        routes_drivers, "get_db_connection",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    resp = client.get("/api/v1/drivers/55/bonus")
    assert resp.status_code == 500


# =================================================================
# GET /{driver_id}/score
# =================================================================

def test_score_returns_summary_and_trend(client, conn):
    conn.fetchrow = AsyncMock(return_value={
        "total_trips": 10, "avg_score": 88.5, "max_score": 99.0,
        "min_score": 70.0, "total_distance_km": 500.0, "total_idle_min": 40.0,
        "total_harsh_brake": 2, "total_harsh_accel": 1,
        "total_harsh_corner": 0, "total_speeding": 1,
    })
    conn.fetch = AsyncMock(return_value=[
        {"month": "2026-06", "trips": 10, "avg_score": 88.5, "min_score": 70.0,
         "total_km": 500.0, "total_harsh_events": 4, "total_idle_min": 40.0},
    ])

    resp = client.get("/api/v1/drivers/55/score")

    assert resp.status_code == 200
    body = resp.json()
    assert body["incentive_tier"] == "B"
    assert body["hr_alert"] is False
    assert len(body["monthly_trend"]) == 1


def test_score_tier_d_triggers_hr_alert(client, conn):
    conn.fetchrow = AsyncMock(return_value={
        "total_trips": 5, "avg_score": 40.0, "max_score": 50.0,
        "min_score": 30.0, "total_distance_km": 100.0, "total_idle_min": 10.0,
        "total_harsh_brake": 5, "total_harsh_accel": 5,
        "total_harsh_corner": 5, "total_speeding": 5,
    })
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/drivers/55/score")

    body = resp.json()
    assert body["incentive_tier"] == "D"
    assert body["hr_alert"] is True


def test_score_no_summary_row_defaults_avg_zero(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/drivers/55/score")

    assert resp.status_code == 200
    assert resp.json()["summary"] == {}


def test_score_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("boom"))

    resp = client.get("/api/v1/drivers/55/score")

    assert resp.status_code == 500


# =================================================================
# GET /{driver_id}/events
# =================================================================

def test_events_returns_paginated_results(client, conn):
    conn.fetch = AsyncMock(side_effect=[
        [{"device_id": "KTC-001"}],  # distinct device_id lookup
        [  # actual events page
            {"ts": "2026-06-01T10:00:00Z", "device_id": "KTC-001", "lat": 13.7,
             "lon": 100.5, "speed": 90.0, "event": "speeding",
             "event_severity": 0.8, "ax": 0.0, "ay": 0.0, "az": 1.0},
        ],
    ])
    conn.fetchval = AsyncMock(return_value=1)

    resp = client.get("/api/v1/drivers/55/events?page=1&limit=10")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["events"]) == 1
    assert body["filters"]["event_type"] is None


def test_events_no_devices_returns_empty(client, conn):
    conn.fetch = AsyncMock(return_value=[])  # distinct device_id -> none

    resp = client.get("/api/v1/drivers/55/events")

    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["total"] == 0


def test_events_filters_by_event_type(client, conn):
    conn.fetch = AsyncMock(side_effect=[
        [{"device_id": "KTC-001"}],
        [],
    ])
    conn.fetchval = AsyncMock(return_value=0)

    resp = client.get("/api/v1/drivers/55/events?event_type=harsh_brake")

    assert resp.status_code == 200
    assert resp.json()["filters"]["event_type"] == "harsh_brake"


def test_events_pagination_limit_boundary_rejected(client):
    resp = client.get("/api/v1/drivers/55/events?limit=1000")
    assert resp.status_code == 422  # le=500 constraint


def test_events_db_error_returns_500(client, conn):
    conn.fetch = AsyncMock(side_effect=RuntimeError("query failed"))

    resp = client.get("/api/v1/drivers/55/events")

    assert resp.status_code == 500


# =================================================================
# GET /{driver_id}/fuel-summary
# =================================================================

def test_fuel_summary_returns_data(client, conn):
    conn.fetchrow = AsyncMock(return_value={
        "total_trips": 12, "total_fuel_used": 45.5, "avg_fuel_per_trip": 3.8,
        "total_distance_km": 400.0, "total_idle_min": 60.0,
        "avg_fuel_per_100km": 11.4, "estimated_idle_fuel_cost_liters": 0.8,
    })

    resp = client.get("/api/v1/drivers/55/fuel-summary?months=3")

    assert resp.status_code == 200
    body = resp.json()
    assert body["driver_id"] == "55"
    assert body["unit"] == "ลิตร"
    assert body["period_months"] == 3


def test_fuel_summary_months_out_of_range_rejected(client):
    resp = client.get("/api/v1/drivers/55/fuel-summary?months=13")
    assert resp.status_code == 422


def test_fuel_summary_no_data_returns_empty_dict_plus_meta(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/drivers/55/fuel-summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["driver_id"] == "55"


def test_fuel_summary_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db exploded"))

    resp = client.get("/api/v1/drivers/55/fuel-summary")

    assert resp.status_code == 500


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))
