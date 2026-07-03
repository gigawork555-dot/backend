# tests/test_trip_manager.py
"""
Coverage target (FDD §14.2): trip_manager.py >= 80%

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

# ── Path bootstrap (same pattern as test_score_calculator.py) ──────
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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


# ── Fixture: reset module-level per-device state between tests ─────
# TRIP_STATE / DEVICE_LOCKS / TRIP_END_TASKS are module-global dicts
# keyed by device_id — without resetting, a device_id reused across
# tests would leak state (is_running, pending debounce task, etc.)
@pytest.fixture(autouse=True)
def _reset_trip_manager_globals():
    TRIP_STATE.clear()
    DEVICE_LOCKS.clear()
    # Cancel and clear any leftover tasks defensively
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
    """Build a fake asyncpg.Connection whose fetchrow() returns a
    given value (a dict behaves fine since the code does dict(row))."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


async def test_get_active_scoring_config_maps_db_columns_to_calculator_keys():
    db_row = {
        "score_base": 100.0,
        "harsh_brake_deduct": 3.0,
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

    assert config["score_base"] == 100.0
    assert config["weight_speeding"] == 10.0
    assert config["weight_harsh_brake"] == 3.0
    assert config["weight_harsh_accel"] == 3.0
    assert config["weight_harsh_corner"] == 3.0
    assert config["weight_idling"] == 2.0
    assert config["weight_bump"] == 4.0
    assert config["speeding_kmh_over"] == 20.0
    assert config["idle_min_threshold"] == 5.0
    assert config["max_deduct_per_trip"] == 50.0
    conn.fetchrow.assert_awaited_once()


def test_harsh_brake_threshold_sign_flipped_from_positive_db_value():
    # DB stores harsh_brake_g as a positive magnitude (0.40); the
    # calculator convention needs it negative (ax < -0.4G)
    pass  # covered inline below via async test to keep asyncio marker


async def test_get_active_scoring_config_flips_brake_threshold_sign():
    db_row = {"harsh_brake_g": 0.40}
    conn = _make_connection(db_row)

    config = await get_active_scoring_config(conn)

    assert config["threshold_harsh_brake"] == -0.40
    assert config["threshold_harsh_accel"] == 0.4  # default when key absent
    assert config["threshold_harsh_corner"] == 0.4


async def test_get_active_scoring_config_defaults_when_row_fields_missing():
    # Row present but with no keys at all -> every .get() falls back
    conn = _make_connection({})

    config = await get_active_scoring_config(conn)

    assert config["score_base"] == 100.0
    assert config["weight_speeding"] == 10.0
    assert config["weight_harsh_brake"] == 3.0
    assert config["weight_bump"] == 4.0
    assert config["max_deduct_per_trip"] == 50.0
    assert config["threshold_harsh_brake"] == -0.4


async def test_get_active_scoring_config_exemption_flags_are_false_fix3():
    # [Fix #3] idling exemption flags must default to False so idling
    # penalties actually apply — verified for both DB-row and fallback
    # branches below
    conn = _make_connection({"score_base": 100.0})

    config = await get_active_scoring_config(conn)

    assert config["enable_traffic_jam_exemption"] is False
    assert config["enable_warehouse_idling_exemption"] is False
    assert config["enable_night_rest_exemption"] is False


async def test_get_active_scoring_config_fallback_when_no_active_config():
    # fetchrow() returns None -> no active row in scoring_config_cache
    conn = _make_connection(None)

    config = await get_active_scoring_config(conn)

    assert config["score_base"] == 100.0
    assert config["weight_speeding"] == 10.0
    assert config["weight_harsh_brake"] == 3.0
    assert config["weight_harsh_accel"] == 3.0
    assert config["weight_harsh_corner"] == 3.0
    assert config["weight_idling"] == 2.0
    assert config["weight_bump"] == 4.0
    assert config["speeding_kmh_over"] == 20.0
    assert config["idle_min_threshold"] == 5.0
    assert config["threshold_harsh_brake"] == -0.4
    assert config["threshold_harsh_accel"] == 0.4
    assert config["threshold_harsh_corner"] == 0.4
    assert config["max_deduct_per_trip"] == 50.0
    # [Fix #3] fallback path must also default exemptions to False
    assert config["enable_traffic_jam_exemption"] is False
    assert config["enable_warehouse_idling_exemption"] is False
    assert config["enable_night_rest_exemption"] is False


# =================================================================
# _haversine_km()
# =================================================================

def test_haversine_km_zero_distance_for_identical_points():
    assert _haversine_km(13.7563, 100.5018, 13.7563, 100.5018) == pytest.approx(0.0, abs=1e-9)


def test_haversine_km_one_degree_latitude_is_about_111_km():
    # 1 degree of latitude ~ 111.19 km on a sphere of radius 6371 km
    # (2*pi*R/360), independent of longitude — a well-known reference value
    dist = _haversine_km(0.0, 0.0, 1.0, 0.0)
    assert dist == pytest.approx(111.1949, abs=0.01)


def test_haversine_km_one_degree_longitude_at_equator_is_about_111_km():
    dist = _haversine_km(0.0, 0.0, 0.0, 1.0)
    assert dist == pytest.approx(111.1949, abs=0.01)


def test_haversine_km_symmetric_regardless_of_point_order():
    d1 = _haversine_km(13.75, 100.50, 18.79, 98.98)   # Bangkok -> Chiang Mai-ish
    d2 = _haversine_km(18.79, 98.98, 13.75, 100.50)
    assert d1 == pytest.approx(d2, abs=1e-9)
    assert d1 > 0


# =================================================================
# _estimate_fuel()
# =================================================================

def test_estimate_fuel_maf_based_branch_used_when_maf_present():
    points = [
        {"maf_airflow": 4.0},
        {"maf_airflow": 6.0},
    ]
    # avg_maf = 5.0, duration = 2 * 5 / 3600 hours
    result = _estimate_fuel(points, distance_km=10.0)
    avg_maf = 5.0
    duration_hr = 2 * 5 / 3600.0
    expected = round(avg_maf * duration_hr / 14.7 * 0.72 / 1000, 2)
    assert result == expected


def test_estimate_fuel_falls_back_to_distance_based_when_no_maf():
    points = [{"maf_airflow": None}, {"speed": 40.0}]
    result = _estimate_fuel(points, distance_km=50.0)
    assert result == pytest.approx(5.0)  # 50 km * 0.10 L/km


def test_estimate_fuel_ignores_zero_or_negative_maf_points():
    points = [{"maf_airflow": 0.0}, {"maf_airflow": -1.0}]
    result = _estimate_fuel(points, distance_km=20.0)
    # both points filtered out (not > 0) -> fallback to distance-based
    assert result == pytest.approx(2.0)


def test_estimate_fuel_empty_points_uses_distance_fallback():
    result = _estimate_fuel([], distance_km=100.0)
    assert result == pytest.approx(10.0)


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
    assert len(track) == 1
    assert track[0]["lat"] == 13.7
    assert track[0]["lon"] == 100.5


def test_build_gps_track_dedupes_consecutive_duplicate_coordinates():
    points = [
        {"lat": 13.7, "lon": 100.5, "ts": "t1", "speed": 0},
        {"lat": 13.7, "lon": 100.5, "ts": "t2", "speed": 0},  # duplicate -> skipped
        {"lat": 13.8, "lon": 100.6, "ts": "t3", "speed": 30},
    ]
    track = _build_gps_track(points)
    assert len(track) == 2
    assert track[0]["lat"] == 13.7
    assert track[1]["lat"] == 13.8


def test_build_gps_track_empty_input_returns_empty_list():
    assert _build_gps_track([]) == []


# =================================================================
# _finalize_trip()
# =================================================================

def _make_pool_with_connection(conn):
    """asyncpg.Pool.acquire() is an async context manager yielding a
    connection — build a MagicMock pool that supports `async with`."""
    pool = MagicMock()

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


async def test_finalize_trip_skips_when_too_few_telemetry_points():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])  # 0 points < MIN_TRIP_POINTS
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    pool = _make_pool_with_connection(conn)

    start = datetime.datetime(2026, 6, 1, 8, 0, 0, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2026, 6, 1, 8, 10, 0, tzinfo=datetime.timezone.utc)

    await _finalize_trip(pool, "KTC-001", start, end)

    # too few points -> must never reach the INSERT INTO trip_logs step
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
            {  # 1st fetchrow call -> get_active_scoring_config()
                "score_base": 100.0,
                "harsh_brake_deduct": 3.0, "harsh_accel_deduct": 3.0,
                "harsh_corner_deduct": 3.0, "speeding_deduct": 10.0,
                "idling_deduct": 2.0, "bump_deduct": 4.0,
                "speeding_kmh_over": 20.0, "idle_min_threshold": 5.0,
                "harsh_brake_g": 0.4, "harsh_accel_g": 0.4, "harsh_corner_g": 0.4,
                "max_deduct_per_trip": 50.0,
            },
            {"vehicle_id": 101, "driver_id": 55},  # 2nd fetchrow -> device_row
        ]
    )
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = _make_pool_with_connection(conn)

    await _finalize_trip(pool, "KTC-001", start, end)

    conn.execute.assert_awaited_once()
    _, call_args, _ = conn.execute.mock_calls[0]
    # positional args after the SQL string
    inserted = call_args[1:]
    device_id, vehicle_id, driver_id = inserted[0], inserted[1], inserted[2]
    assert device_id == "KTC-001"
    assert vehicle_id == 101
    assert driver_id == 55


# =================================================================
# handle_telemetry() — ignition state machine + debounce
# =================================================================

async def test_handle_telemetry_ignition_on_starts_trip():
    pool = MagicMock()
    start_ts = 1750000000

    await handle_telemetry(pool, {"device_id": "KTC-101", "ignition": True, "ts": start_ts})

    state = TRIP_STATE["KTC-101"]
    assert state.is_running is True
    assert state.start_time == datetime.datetime.fromtimestamp(
        start_ts, tz=datetime.timezone.utc
    )


async def test_handle_telemetry_missing_device_id_is_noop():
    pool = MagicMock()
    # should return silently without raising and without touching state
    await handle_telemetry(pool, {"ignition": True})
    assert TRIP_STATE == {}


async def test_handle_telemetry_ignition_off_then_on_within_debounce_cancels_finalize(monkeypatch):
    """
    FDD requirement: ignition OFF then back ON *within* the 30s debounce
    window must cancel the pending finalize — _finalize_trip must never
    be called.
    """
    # Shrink the debounce window so the test doesn't need to sleep 30s
    monkeypatch.setattr(trip_manager, "DEBOUNCE_SECONDS", 0.2)

    finalize_mock = AsyncMock()
    monkeypatch.setattr(trip_manager, "_finalize_trip", finalize_mock)

    pool = MagicMock()
    device_id = "KTC-DEBOUNCE-1"

    # 1) ignition ON -> trip starts
    await handle_telemetry(pool, {"device_id": device_id, "ignition": True, "ts": 1750000000})
    assert TRIP_STATE[device_id].is_running is True

    # 2) ignition OFF -> debounce task scheduled
    await handle_telemetry(pool, {"device_id": device_id, "ignition": False, "ts": 1750000010})
    assert device_id in TRIP_END_TASKS

    # 3) ignition ON again, well BEFORE the (shrunk) debounce window elapses
    await asyncio.sleep(0.05)
    await handle_telemetry(pool, {"device_id": device_id, "ignition": True, "ts": 1750000015})

    # debounce task must have been popped/cancelled
    assert device_id not in TRIP_END_TASKS
    assert TRIP_STATE[device_id].is_running is True
    assert TRIP_STATE[device_id].last_ignition_off_time is None

    # give the cancelled task a chance to actually unwind
    await asyncio.sleep(0.3)

    finalize_mock.assert_not_awaited()


async def test_handle_telemetry_ignition_off_debounce_elapses_calls_finalize(monkeypatch):
    """
    Mirror scenario: ignition OFF and left off past the debounce window
    -> _finalize_trip IS called exactly once with the recorded
    start/end times.
    """
    monkeypatch.setattr(trip_manager, "DEBOUNCE_SECONDS", 0.1)

    finalize_mock = AsyncMock()
    monkeypatch.setattr(trip_manager, "_finalize_trip", finalize_mock)

    pool = MagicMock()
    device_id = "KTC-DEBOUNCE-2"

    await handle_telemetry(pool, {"device_id": device_id, "ignition": True, "ts": 1750000000})
    await handle_telemetry(pool, {"device_id": device_id, "ignition": False, "ts": 1750000010})

    assert device_id in TRIP_END_TASKS

    # wait past the (shrunk) debounce window
    await asyncio.sleep(0.3)

    finalize_mock.assert_awaited_once()
    # state should have been reset by _debounce_and_finalize after firing
    assert TRIP_STATE[device_id].is_running is False
    assert device_id not in TRIP_END_TASKS


async def test_handle_telemetry_ignition_off_twice_does_not_schedule_second_task(monkeypatch):
    # if a debounce task is already pending for this device, a second
    # ignition-OFF message must not schedule a duplicate task
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

    assert first_task is second_task

    # cleanup: cancel to avoid leaking a live task past the test
    first_task.cancel()
    await asyncio.sleep(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))
