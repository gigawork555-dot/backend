# tests/test_trip_manager.py
"""
Coverage target (FDD §14.2): trip_manager.py >= 80%

[แก้ไข] ทุก assert ถูกแทนที่ด้วย check()/check_approx() จาก conftest.py
เพื่อ print ค่า actual/expected จริงก่อนเช็ค — รันด้วย `-v -s`:

    docker compose run --rm backend pytest tests/test_trip_manager.py -v -s

Covers:
- get_active_scoring_config() maps scoring_config_cache columns -> the
  exact keys calculate_advanced_trip_score() reads (score_base, weight_*,
  threshold_harsh_*, max_deduct_per_trip, exemption flags == False per
  Fix #3, weight_bump per Fix #4)
- get_active_scoring_config() fallback path when no active row in DB
- _haversine_km() against known reference distances
- _estimate_fuel(): MAF-based branch and distance-based fallback branch
- _build_gps_track(): dedupes consecutive duplicate coordinates, skips
  points with missing lat/lon
- _finalize_trip(): end-to-end INSERT path with mocked pool/connection,
  plus the "too few points -> skip" early-return branch
- handle_telemetry() ignition ON -> starts trip; ignition OFF -> starts
  DEBOUNCE_SECONDS debounce task; ignition ON again *before* the debounce
  window elapses cancels the pending finalize (no _finalize_trip call)
- handle_telemetry() ignition OFF -> debounce elapses without being
  cancelled -> _finalize_trip *is* called

Uses pytest-asyncio and unittest.mock (AsyncMock/MagicMock) to fully
mock asyncpg.Pool / asyncpg.Connection — no real database required.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock

# ── Path bootstrap ──────────────────────────────────────────────
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TEST_DIR = os.path.dirname(__file__)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from conftest import check, check_approx  # noqa: E402

import app.services.trip_manager as trip_manager  # noqa: E402
from app.services.trip_manager import (  # noqa: E402
    get_active_scoring_config,
    _haversine_km,
    _estimate_fuel,
    _build_gps_track,
    _finalize_trip,
    handle_telemetry,
    TRIP_STATE,
    DEVICE_LOCKS,
    TRIP_END_TASKS,
)


@pytest.fixture(autouse=True)
def _reset_trip_manager_globals():
    TRIP_STATE.clear()
    DEVICE_LOCKS.clear()
    for task in list(TRIP_END_TASKS.values()):
        if not task.done():
            task.cancel()
    TRIP_END_TASKS.clear()
    yield
    for task in list(TRIP_END_TASKS.values()):
        if not task.done():
            task.cancel()
    TRIP_END_TASKS.clear()
    TRIP_STATE.clear()
    DEVICE_LOCKS.clear()


# =================================================================
# get_active_scoring_config()
# =================================================================

def _make_connection(fetchrow_return):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


async def test_get_active_scoring_config_maps_db_columns_to_calculator_keys():
    db_row = {
        "score_base": 100.0,
        "harsh_brake_deduct": 5.0,
        "harsh_accel_deduct": 3.0,
        "harsh_corner_deduct": 3.0,
        "speeding_deduct": 10.0,
        "idling_deduct": 2.0,
        "bump_deduct": 4.0,
        "speeding_kmh_over": 20.0,
        "idle_min_threshold": 5.0,
        "harsh_brake_g": 0.40,
        "harsh_accel_g": 0.40,
        "harsh_corner_g": 0.40,
        "max_deduct_per_trip": 50.0,
    }
    conn = _make_connection(db_row)

    config = await get_active_scoring_config(conn)

    check("config['score_base']", config["score_base"], 100.0)
    check("config['weight_speeding']", config["weight_speeding"], 10.0)
    check("config['weight_harsh_brake']", config["weight_harsh_brake"], 5.0)  
    check("config['weight_harsh_accel']", config["weight_harsh_accel"], 3.0)
    check("config['weight_harsh_corner']", config["weight_harsh_corner"], 3.0)
    check("config['weight_idling']", config["weight_idling"], 2.0)
    check("config['weight_bump']", config["weight_bump"], 4.0)
    check("config['speeding_kmh_over']", config["speeding_kmh_over"], 20.0)
    check("config['idle_min_threshold']", config["idle_min_threshold"], 5.0)
    check("config['max_deduct_per_trip']", config["max_deduct_per_trip"], 50.0)
    print(f"  🔎 conn.fetchrow await count     -> actual={conn.fetchrow.await_count} expected=1")
    conn.fetchrow.assert_awaited_once()


async def test_get_active_scoring_config_flips_brake_threshold_sign():
    db_row = {"harsh_brake_g": 0.40}
    conn = _make_connection(db_row)

    config = await get_active_scoring_config(conn)

    check("config['threshold_harsh_brake']", config["threshold_harsh_brake"], -0.40)
    check("config['threshold_harsh_accel']", config["threshold_harsh_accel"], 0.4)
    check("config['threshold_harsh_corner']", config["threshold_harsh_corner"], 0.4)


async def test_get_active_scoring_config_defaults_when_row_fields_missing():
    conn = _make_connection({})

    config = await get_active_scoring_config(conn)

    check("config['score_base']", config["score_base"], 100.0)
    check("config['weight_speeding']", config["weight_speeding"], 10.0)
    check("config['weight_harsh_brake']", config["weight_harsh_brake"], 5.0)
    check("config['weight_bump']", config["weight_bump"], 4.0)
    check("config['max_deduct_per_trip']", config["max_deduct_per_trip"], 50.0)
    check("config['threshold_harsh_brake']", config["threshold_harsh_brake"], -0.4)


async def test_get_active_scoring_config_exemption_flags_are_false_fix3():
    conn = _make_connection({"score_base": 100.0})

    config = await get_active_scoring_config(conn)

    check("config['enable_traffic_jam_exemption']", config["enable_traffic_jam_exemption"], False)
    check("config['enable_warehouse_idling_exemption']", config["enable_warehouse_idling_exemption"], False)
    check("config['enable_night_rest_exemption']", config["enable_night_rest_exemption"], False)


async def test_get_active_scoring_config_fallback_when_no_active_config():
    conn = _make_connection(None)

    config = await get_active_scoring_config(conn)

    check("config['score_base']", config["score_base"], 100.0)
    check("config['weight_speeding']", config["weight_speeding"], 10.0)
    check("config['weight_harsh_brake']", config["weight_harsh_brake"], 5.0)
    check("config['weight_harsh_accel']", config["weight_harsh_accel"], 3.0)
    check("config['weight_harsh_corner']", config["weight_harsh_corner"], 3.0)
    check("config['weight_idling']", config["weight_idling"], 2.0)
    check("config['weight_bump']", config["weight_bump"], 4.0)
    check("config['speeding_kmh_over']", config["speeding_kmh_over"], 20.0)
    check("config['idle_min_threshold']", config["idle_min_threshold"], 5.0)
    check("config['threshold_harsh_brake']", config["threshold_harsh_brake"], -0.4)
    check("config['threshold_harsh_accel']", config["threshold_harsh_accel"], 0.4)
    check("config['threshold_harsh_corner']", config["threshold_harsh_corner"], 0.4)
    check("config['max_deduct_per_trip']", config["max_deduct_per_trip"], 50.0)
    check("config['enable_traffic_jam_exemption']", config["enable_traffic_jam_exemption"], False)
    check("config['enable_warehouse_idling_exemption']", config["enable_warehouse_idling_exemption"], False)
    check("config['enable_night_rest_exemption']", config["enable_night_rest_exemption"], False)


# =================================================================
# _haversine_km()
# =================================================================

def test_haversine_km_zero_distance_for_identical_points():
    dist = _haversine_km(13.7563, 100.5018, 13.7563, 100.5018)
    check_approx("_haversine_km(same point)", dist, 0.0, abs_tol=1e-9)


def test_haversine_km_one_degree_latitude_is_about_111_km():
    dist = _haversine_km(0.0, 0.0, 1.0, 0.0)
    check_approx("_haversine_km(1° lat)", dist, 111.1949, abs_tol=0.01)


def test_haversine_km_one_degree_longitude_at_equator_is_about_111_km():
    dist = _haversine_km(0.0, 0.0, 0.0, 1.0)
    check_approx("_haversine_km(1° lon @ equator)", dist, 111.1949, abs_tol=0.01)


def test_haversine_km_symmetric_regardless_of_point_order():
    d1 = _haversine_km(13.75, 100.50, 18.79, 98.98)
    d2 = _haversine_km(18.79, 98.98, 13.75, 100.50)
    check_approx("_haversine_km(A->B) vs (B->A)", d1, d2, abs_tol=1e-9)
    print(f"  🔎 distance > 0                 -> actual={d1!r}")
    assert d1 > 0


# =================================================================
# _estimate_fuel()
# =================================================================

def test_estimate_fuel_maf_based_branch_used_when_maf_present():
    points = [
        {"maf_airflow": 4.0},
        {"maf_airflow": 6.0},
    ]
    result = _estimate_fuel(points, distance_km=10.0)
    avg_maf = 5.0
    duration_hr = 2 * 5 / 3600.0
    expected = round(avg_maf * duration_hr / 14.7 * 0.72 / 1000, 2)
    check("_estimate_fuel(MAF branch)", result, expected)


def test_estimate_fuel_falls_back_to_distance_based_when_no_maf():
    points = [{"maf_airflow": None}, {"speed": 40.0}]
    result = _estimate_fuel(points, distance_km=50.0)
    check_approx("_estimate_fuel(no MAF)", result, 5.0)


def test_estimate_fuel_ignores_zero_or_negative_maf_points():
    points = [{"maf_airflow": 0.0}, {"maf_airflow": -1.0}]
    result = _estimate_fuel(points, distance_km=20.0)
    check_approx("_estimate_fuel(zero/neg MAF filtered)", result, 2.0)


def test_estimate_fuel_empty_points_uses_distance_fallback():
    result = _estimate_fuel([], distance_km=100.0)
    check_approx("_estimate_fuel(empty points)", result, 10.0)


# =================================================================
# _build_gps_track()
# =================================================================

def test_build_gps_track_skips_points_missing_lat_or_lon():
    points = [
        {"lat": None, "lon": 100.0, "ts": "t1", "speed": 10},
        {"lat": 13.7, "lon": None, "ts": "t2", "speed": 10},
        {"lat": 13.7, "lon": 100.5, "ts": "t3", "speed": 20},
    ]
    track = _build_gps_track(points)
    check("len(track)", len(track), 1)
    check("track[0]['lat']", track[0]["lat"], 13.7)
    check("track[0]['lon']", track[0]["lon"], 100.5)


def test_build_gps_track_dedupes_consecutive_duplicate_coordinates():
    points = [
        {"lat": 13.7, "lon": 100.5, "ts": "t1", "speed": 0},
        {"lat": 13.7, "lon": 100.5, "ts": "t2", "speed": 0},
        {"lat": 13.8, "lon": 100.6, "ts": "t3", "speed": 30},
    ]
    track = _build_gps_track(points)
    check("len(track)", len(track), 2)
    check("track[0]['lat']", track[0]["lat"], 13.7)
    check("track[1]['lat']", track[1]["lat"], 13.8)


def test_build_gps_track_empty_input_returns_empty_list():
    check("_build_gps_track([])", _build_gps_track([]), [])


# =================================================================
# _finalize_trip()
# =================================================================

def _make_pool_with_connection(conn):
    pool = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


async def test_finalize_trip_skips_when_too_few_telemetry_points():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    pool = _make_pool_with_connection(conn)

    start = datetime.datetime(2026, 6, 1, 8, 0, 0, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2026, 6, 1, 8, 10, 0, tzinfo=datetime.timezone.utc)

    await _finalize_trip(pool, "KTC-001", start, end)

    print(f"  🔎 conn.execute await count      -> actual={conn.execute.await_count} expected=0 (skipped)")
    conn.execute.assert_not_awaited()


async def test_finalize_trip_inserts_trip_log_with_computed_metrics():
    start = datetime.datetime(2026, 6, 1, 8, 0, 0, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2026, 6, 1, 8, 10, 0, tzinfo=datetime.timezone.utc)

    telemetry_rows = [
        {
            "ts": start, "lat": 13.75, "lon": 100.50, "speed": 40.0, "heading": 90,
            "rpm": 2000, "throttle": 30, "engine_load": 50, "fuel_level": 70,
            "maf_airflow": 5.0, "ax": 0.0, "ay": 0.0, "az": 1.0,
            "gx": 0.0, "gy": 0.0, "gz": 0.0,
            "event": None, "event_severity": 0.0, "ignition": True,
        },
        {
            "ts": end, "lat": 13.80, "lon": 100.55, "speed": 0.0, "heading": 90,
            "rpm": 0, "throttle": 0, "engine_load": 0, "fuel_level": 69,
            "maf_airflow": 5.0, "ax": 0.0, "ay": 0.0, "az": 1.0,
            "gx": 0.0, "gy": 0.0, "gz": 0.0,
            "event": None, "event_severity": 0.0, "ignition": False,
        },
    ]

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=telemetry_rows)
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "score_base": 100.0,
                "harsh_brake_deduct": 3.0, "harsh_accel_deduct": 3.0,
                "harsh_corner_deduct": 3.0, "speeding_deduct": 10.0,
                "idling_deduct": 2.0, "bump_deduct": 4.0,
                "speeding_kmh_over": 20.0, "idle_min_threshold": 5.0,
                "harsh_brake_g": 0.4, "harsh_accel_g": 0.4, "harsh_corner_g": 0.4,
                "max_deduct_per_trip": 50.0,
            },
            {"vehicle_id": 101, "driver_id": 55},
        ]
    )
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = _make_pool_with_connection(conn)

    await _finalize_trip(pool, "KTC-001", start, end)

    print(f"  🔎 conn.execute await count      -> actual={conn.execute.await_count} expected=1")
    conn.execute.assert_awaited_once()
    _, call_args, _ = conn.execute.mock_calls[0]
    inserted = call_args[1:]
    device_id, vehicle_id, driver_id = inserted[0], inserted[1], inserted[2]
    check("inserted device_id", device_id, "KTC-001")
    check("inserted vehicle_id", vehicle_id, 101)
    check("inserted driver_id", driver_id, 55)


# =================================================================
# handle_telemetry() — ignition state machine + debounce
# =================================================================

async def test_handle_telemetry_ignition_on_starts_trip():
    pool = MagicMock()
    start_ts = 1750000000

    await handle_telemetry(pool, {"device_id": "KTC-101", "ignition": True, "ts": start_ts})

    state = TRIP_STATE["KTC-101"]
    expected_start = datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc)
    check("state.is_running", state.is_running, True)
    check("state.start_time", state.start_time, expected_start)


async def test_handle_telemetry_missing_device_id_is_noop():
    pool = MagicMock()
    await handle_telemetry(pool, {"ignition": True})
    check("TRIP_STATE", TRIP_STATE, {})


async def test_handle_telemetry_ignition_off_then_on_within_debounce_cancels_finalize(monkeypatch):
    monkeypatch.setattr(trip_manager, "DEBOUNCE_SECONDS", 0.2)

    finalize_mock = AsyncMock()
    monkeypatch.setattr(trip_manager, "_finalize_trip", finalize_mock)

    pool = MagicMock()
    device_id = "KTC-DEBOUNCE-1"

    await handle_telemetry(pool, {"device_id": device_id, "ignition": True, "ts": 1750000000})
    check("state.is_running (after ON)", TRIP_STATE[device_id].is_running, True)

    await handle_telemetry(pool, {"device_id": device_id, "ignition": False, "ts": 1750000010})
    print(f"  🔎 device_id in TRIP_END_TASKS   -> actual={device_id in TRIP_END_TASKS} expected=True")
    assert device_id in TRIP_END_TASKS

    await asyncio.sleep(0.05)
    await handle_telemetry(pool, {"device_id": device_id, "ignition": True, "ts": 1750000015})

    check("device_id in TRIP_END_TASKS (after re-ON)", device_id in TRIP_END_TASKS, False)
    check("state.is_running (after re-ON)", TRIP_STATE[device_id].is_running, True)
    check("state.last_ignition_off_time", TRIP_STATE[device_id].last_ignition_off_time, None)

    await asyncio.sleep(0.3)

    print(f"  🔎 finalize_mock await count     -> actual={finalize_mock.await_count} expected=0")
    finalize_mock.assert_not_awaited()


async def test_handle_telemetry_ignition_off_debounce_elapses_calls_finalize(monkeypatch):
    monkeypatch.setattr(trip_manager, "DEBOUNCE_SECONDS", 0.1)

    finalize_mock = AsyncMock()
    monkeypatch.setattr(trip_manager, "_finalize_trip", finalize_mock)

    pool = MagicMock()
    device_id = "KTC-DEBOUNCE-2"

    await handle_telemetry(pool, {"device_id": device_id, "ignition": True, "ts": 1750000000})
    await handle_telemetry(pool, {"device_id": device_id, "ignition": False, "ts": 1750000010})

    print(f"  🔎 device_id in TRIP_END_TASKS   -> actual={device_id in TRIP_END_TASKS} expected=True")
    assert device_id in TRIP_END_TASKS

    await asyncio.sleep(0.3)

    print(f"  🔎 finalize_mock await count     -> actual={finalize_mock.await_count} expected=1")
    finalize_mock.assert_awaited_once()
    check("state.is_running (after finalize)", TRIP_STATE[device_id].is_running, False)
    check("device_id in TRIP_END_TASKS (after finalize)", device_id in TRIP_END_TASKS, False)


async def test_handle_telemetry_ignition_off_twice_does_not_schedule_second_task(monkeypatch):
    monkeypatch.setattr(trip_manager, "DEBOUNCE_SECONDS", 0.3)
    finalize_mock = AsyncMock()
    monkeypatch.setattr(trip_manager, "_finalize_trip", finalize_mock)

    pool = MagicMock()
    device_id = "KTC-DEBOUNCE-3"

    await handle_telemetry(pool, {"device_id": device_id, "ignition": True, "ts": 1750000000})
    await handle_telemetry(pool, {"device_id": device_id, "ignition": False, "ts": 1750000010})
    first_task = TRIP_END_TASKS[device_id]

    await handle_telemetry(pool, {"device_id": device_id, "ignition": False, "ts": 1750000011})
    second_task = TRIP_END_TASKS[device_id]

    print(f"  🔎 first_task is second_task     -> actual={first_task is second_task} expected=True")
    assert first_task is second_task

    first_task.cancel()
    await asyncio.sleep(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))