# tests/api_test_routes_reports.py
"""
Coverage target: app/api/routes_reports.py
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

_TEST_DIR = os.path.dirname(__file__)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from conftest import check, check_is, check_approx  # noqa: E402

from app.api import routes_reports  # noqa: E402

VALID_KEY = "ktc-fleet-2026-secret"


# =================================================================
# Fixtures
# =================================================================

def _make_conn(fetch_return=None, fetchval_return=0):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.close = AsyncMock(return_value=None)
    return conn


@pytest.fixture
def conn():
    return _make_conn()


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setattr(routes_reports, "_get_db", AsyncMock(return_value=conn))
    app = FastAPI()
    app.include_router(routes_reports.router)
    return TestClient(app, headers={"APIKEY": VALID_KEY})


# =================================================================
# Auth
# =================================================================

def test_driver_score_rejects_missing_key(conn, monkeypatch):
    monkeypatch.setattr(routes_reports, "_get_db", AsyncMock(return_value=conn))
    app = FastAPI()
    app.include_router(routes_reports.router)
    client = TestClient(app)

    resp = client.get("/api/v1/reports/driver-score")

    check("resp.status_code (no key)", resp.status_code, 403)


def test_driver_score_rejects_wrong_key(conn, monkeypatch):
    monkeypatch.setattr(routes_reports, "_get_db", AsyncMock(return_value=conn))
    app = FastAPI()
    app.include_router(routes_reports.router)
    client = TestClient(app, headers={"APIKEY": "wrong"})

    resp = client.get("/api/v1/reports/driver-score")

    check("resp.status_code (wrong key)", resp.status_code, 403)


# =================================================================
# GET /api/v1/reports/driver-score
# =================================================================

def test_driver_score_report_assigns_tiers(client, conn):
    conn.fetchval = AsyncMock(return_value=2)
    conn.fetch = AsyncMock(return_value=[
        {"driver_id": 1, "month": "2026-06", "total_trips": 5, "avg_score": 95.0,
         "min_score": 90.0, "safe_trips": 5, "total_harsh_brake": 0,
         "total_harsh_accel": 0, "total_harsh_corner": 0, "total_speeding": 0,
         "total_idle_min": 1.0, "total_distance_km": 50.0},
        {"driver_id": 2, "month": "2026-06", "total_trips": 3, "avg_score": 55.0,
         "min_score": 40.0, "safe_trips": 0, "total_harsh_brake": 5,
         "total_harsh_accel": 3, "total_harsh_corner": 2, "total_speeding": 1,
         "total_idle_min": 10.0, "total_distance_km": 20.0},
    ])

    resp = client.get("/api/v1/reports/driver-score")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    tiers = {r["driver_id"]: r["incentive_tier"] for r in body["data"]}
    check("tiers[1]", tiers[1], "A")
    check("tiers[2]", tiers[2], "D")


def test_driver_score_report_filters_single_driver(client, conn):
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/reports/driver-score?driver_id=42")

    check("resp.status_code", resp.status_code, 200)
    _, call_args, _ = conn.fetch.mock_calls[0]
    print(f"  🔎 {'42 in bound params':<28} -> actual={42 in call_args}")
    assert 42 in call_args


def test_driver_score_report_pagination_metadata(client, conn):
    conn.fetchval = AsyncMock(return_value=25)
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/reports/driver-score?page=2&limit=10")

    body = resp.json()
    check("body['page']", body["page"], 2)
    check("body['total_pages'] (ceil 25/10)", body["total_pages"], 3)


def test_driver_score_report_db_error_returns_500(client, conn):
    conn.fetchval = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.get("/api/v1/reports/driver-score")

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# GET /api/v1/reports/fleet-summary
# =================================================================

def test_fleet_summary_returns_daily_rows(client, conn):
    conn.fetch = AsyncMock(return_value=[
        {"date": "2026-06-01", "total_trips": 10, "active_vehicles": 3,
         "active_drivers": 3, "avg_score": 88.0, "total_distance_km": 300.0,
         "total_harsh_events": 5, "total_speeding": 1, "total_idle_min": 20.0,
         "total_fuel_used": 25.0},
    ])

    resp = client.get("/api/v1/reports/fleet-summary?days=7")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['total_days']", body["total_days"], 1)
    check("body['days']", body["days"], 7)


def test_fleet_summary_days_out_of_range_rejected(client):
    resp = client.get("/api/v1/reports/fleet-summary?days=1000")
    check("resp.status_code (days>365)", resp.status_code, 422)


def test_fleet_summary_db_error_returns_500(client, conn):
    conn.fetch = AsyncMock(side_effect=RuntimeError("boom"))

    resp = client.get("/api/v1/reports/fleet-summary")

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# GET /api/v1/reports/fuel-efficiency
# =================================================================

def test_fuel_efficiency_returns_per_vehicle_rows(client, conn):
    conn.fetch = AsyncMock(return_value=[
        {"vehicle_id": 101, "total_trips": 8, "total_fuel_used": 40.0,
         "total_distance_km": 350.0, "fuel_per_100km": 11.4,
         "avg_driver_score": 90.0, "total_idle_min": 15.0,
         "idle_fuel_est_liters": 0.2},
    ])

    resp = client.get("/api/v1/reports/fuel-efficiency?days=30")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['total_vehicles']", body["total_vehicles"], 1)
    check("body['unit']", body["unit"], "ลิตร")


def test_fuel_efficiency_empty_result(client, conn):
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/reports/fuel-efficiency")

    check("resp.status_code", resp.status_code, 200)
    check("body['data']", resp.json()["data"], [])


def test_fuel_efficiency_db_error_returns_500(client, conn):
    conn.fetch = AsyncMock(side_effect=RuntimeError("db exploded"))

    resp = client.get("/api/v1/reports/fuel-efficiency")

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# GET /api/v1/reports/maintenance-forecast
# =================================================================

def test_maintenance_forecast_flags_vehicles_needing_service(client, conn):
    conn.fetch = AsyncMock(return_value=[
        {"vehicle_id": 101, "total_trips": 50, "total_distance_km": 6000.0,
         "total_duration_min": 3000.0, "total_engine_hours": 50.0,
         "total_harsh_brake": 25, "total_harsh_accel": 5,
         "total_harsh_corner": 3, "avg_score": 70.0,
         "last_trip": None, "days_since_last_trip": 100,
         "distance_priority": "สูง", "engine_hours_priority": "ต่ำ",
         "needs_maintenance": True},
    ])

    resp = client.get("/api/v1/reports/maintenance-forecast")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['needs_maintenance']", body["needs_maintenance"], 1)
    reasons = body["data"][0]["maintenance_reasons"]
    print(f"  🔎 {'reasons contains ระยะทาง':<28} -> actual={any('ระยะทาง' in r for r in reasons)}")
    assert any("ระยะทาง" in r for r in reasons)
    print(f"  🔎 {'reasons contains เบรคหัก':<28} -> actual={any('เบรคหัก' in r for r in reasons)}")
    assert any("เบรคหัก" in r for r in reasons)


def test_maintenance_forecast_custom_thresholds_reflected_in_response(client, conn):
    conn.fetch = AsyncMock(return_value=[])

    resp = client.get(
        "/api/v1/reports/maintenance-forecast?km_high=1000&km_medium=500"
    )

    check("resp.status_code", resp.status_code, 200)
    thresholds = resp.json()["thresholds_used"]
    check("thresholds['trigger_1_distance']['high']", thresholds["trigger_1_distance"]["high"], 1000)
    check("thresholds['trigger_1_distance']['medium']", thresholds["trigger_1_distance"]["medium"], 500)


def test_maintenance_forecast_no_vehicles_needing_service(client, conn):
    conn.fetch = AsyncMock(return_value=[
        {"vehicle_id": 101, "total_trips": 5, "total_distance_km": 100.0,
         "total_duration_min": 60.0, "total_engine_hours": 1.0,
         "total_harsh_brake": 0, "total_harsh_accel": 0,
         "total_harsh_corner": 0, "avg_score": 95.0,
         "last_trip": None, "days_since_last_trip": 1,
         "distance_priority": "ต่ำ", "engine_hours_priority": "ต่ำ",
         "needs_maintenance": False},
    ])

    resp = client.get("/api/v1/reports/maintenance-forecast")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['needs_maintenance']", body["needs_maintenance"], 0)
    check("body['data'][0]['maintenance_reasons']", body["data"][0]["maintenance_reasons"], [])


def test_maintenance_forecast_db_error_returns_500(client, conn):
    conn.fetch = AsyncMock(side_effect=RuntimeError("query error"))

    resp = client.get("/api/v1/reports/maintenance-forecast")

    check("resp.status_code (db error)", resp.status_code, 500)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))