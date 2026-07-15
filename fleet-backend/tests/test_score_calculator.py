# tests/test_score_calculator.py
"""
Coverage target (FDD §14.2): score_calculator.py >= 95%

[แก้ไข] ทุก assert ถูกแทนที่ด้วย check()/check_approx()/check_range()
จาก conftest.py เพื่อ print ค่า actual/expected จริงก่อนเช็ค
รันด้วย `-v -s` เพื่อดูค่า:

    docker compose run --rm backend pytest tests/test_score_calculator.py -v -s

Covers:
- empty telemetry -> score_base passthrough
- each weight/event type individually (speeding, harsh_brake,
  harsh_acceleration, harsh_cornering, harsh bump, idling)
- clamp behaviour (0 floor, score_base ceiling)
- max_deduct_per_trip cap
- night_danger_zone_multiplier (00:00-04:00 window)
- mountain road exemption (lat 18.5-19.5) for brake/corner
- low-speed brake exemption (construction/accident zone, speed < 20)
- FSM debounce: consecutive identical events only counted once
- missing/malformed input handling (no exceptions)
"""

import datetime
import os
import sys

import pytest

# ── Path bootstrap ──────────────────────────────────────────────
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TEST_DIR = os.path.dirname(__file__)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from conftest import check, check_approx, check_range  # noqa: E402

from app.services.score_calculator import calculate_advanced_trip_score  # noqa: E402

DEFAULT_CONFIG = {
    "score_base": 100.0,
    "weight_speeding": 5.0,
    "weight_harsh_brake": 3.0,
    "weight_harsh_accel": 3.0,
    "weight_harsh_corner": 2.0,
    "weight_idling": 1.0,
    "weight_bump": 4.0,
    "idle_min_threshold": 5.0,
    "max_deduct_per_trip": 100.0,
    "night_danger_zone_multiplier": 1.5,
    "enable_construction_zone_exemption": False,
    "enable_accident_delay_exemption": False,
    "enable_mountain_road_exemption": False,
    "enable_traffic_jam_exemption": False,
    "enable_warehouse_idling_exemption": False,
    "enable_night_rest_exemption": False,
}


def cfg(**overrides):
    merged = dict(DEFAULT_CONFIG)
    merged.update(overrides)
    return merged


def point(**kwargs):
    base = {
        "speed": 0.0,
        "lat": 13.7563,
        "ts": datetime.datetime(2026, 6, 1, 12, 0, 0),
        "event": None,
        "ignition": False,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------

def test_empty_telemetry_returns_score_base():
    result = calculate_advanced_trip_score([], cfg(score_base=88.0))
    check("result['safety_score']", result["safety_score"], 88.0)
    check("result['metrics']", result["metrics"], {})


def test_empty_telemetry_defaults_to_100_when_no_score_base_key():
    result = calculate_advanced_trip_score([], {})
    check("result['safety_score']", result["safety_score"], 100.0)


# ---------------------------------------------------------------
# Single-event penalties
# ---------------------------------------------------------------

def test_speeding_event_applies_penalty_and_counts_once():
    data = [point(event="speeding", speed=110)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['speeding_count']", result["metrics"]["speeding_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - 5.0)


def test_harsh_brake_event_applies_penalty():
    data = [point(event="harsh_brake", speed=40)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['harsh_brake_count']", result["metrics"]["harsh_brake_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - 3.0)


def test_harsh_acceleration_event_applies_penalty():
    data = [point(event="harsh_acceleration", speed=40)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['harsh_accel_count']", result["metrics"]["harsh_accel_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - 3.0)


def test_harsh_cornering_event_applies_penalty():
    data = [point(event="harsh_cornering", speed=40)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['harsh_corner_count']", result["metrics"]["harsh_corner_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - 2.0)


def test_harsh_bump_event_applies_penalty():
    data = [point(event="bump", speed=40)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['bump_count']", result["metrics"]["bump_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - 4.0)


def test_harsh_bump_debounce_counts_once_across_consecutive_samples():
    data = [point(event="bump", speed=40) for _ in range(4)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['bump_count']", result["metrics"]["bump_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - 4.0)


def test_idling_penalty_applied_when_no_exemption_active():
    start = datetime.datetime(2026, 6, 1, 12, 0, 0)
    end = start + datetime.timedelta(minutes=10)
    data = [
        point(ts=start, ignition=True, speed=0.0),
        point(ts=end, ignition=False, speed=30.0),
    ]
    result = calculate_advanced_trip_score(data, cfg())
    check_approx("metrics['engine_idle_minutes']", result["metrics"]["engine_idle_minutes"], 10.0)
    check_approx("safety_score", result["safety_score"], 100.0 - 5.0)


def test_idling_penalty_zero_when_exempted():
    start = datetime.datetime(2026, 6, 1, 12, 0, 0)
    end = start + datetime.timedelta(minutes=10)
    data = [
        point(ts=start, ignition=True, speed=0.0),
        point(ts=end, ignition=False, speed=30.0),
    ]
    result = calculate_advanced_trip_score(
        data, cfg(enable_traffic_jam_exemption=True)
    )
    check_approx("safety_score", result["safety_score"], 100.0)


def test_idling_penalty_not_applied_below_threshold():
    start = datetime.datetime(2026, 6, 1, 12, 0, 0)
    end = start + datetime.timedelta(minutes=3)
    data = [
        point(ts=start, ignition=True, speed=0.0),
        point(ts=end, ignition=False, speed=30.0),
    ]
    result = calculate_advanced_trip_score(data, cfg())
    check_approx("safety_score", result["safety_score"], 100.0)


def test_idling_open_segment_closed_at_end_of_telemetry():
    start = datetime.datetime(2026, 6, 1, 12, 0, 0)
    mid = start + datetime.timedelta(minutes=8)
    data = [
        point(ts=start, ignition=True, speed=0.0),
        point(ts=mid, ignition=True, speed=0.0),
    ]
    result = calculate_advanced_trip_score(data, cfg())
    check_approx("metrics['engine_idle_minutes']", result["metrics"]["engine_idle_minutes"], 8.0)


def test_idling_never_starts_when_ignition_false():
    start = datetime.datetime(2026, 6, 1, 12, 0, 0)
    mid = start + datetime.timedelta(minutes=8)
    data = [
        point(ts=start, ignition=False, speed=0.0),
        point(ts=mid, ignition=False, speed=0.0),
    ]
    result = calculate_advanced_trip_score(data, cfg())
    check_approx("metrics['engine_idle_minutes']", result["metrics"]["engine_idle_minutes"], 0.0)


# ---------------------------------------------------------------
# Clamp behaviour
# ---------------------------------------------------------------

def test_score_never_goes_below_zero():
    data = [point(event="speeding", speed=200) for _ in range(3)]
    interleaved = []
    for p in data:
        interleaved.append(p)
        interleaved.append(point(event=None))
    result = calculate_advanced_trip_score(
        interleaved, cfg(weight_speeding=1000.0, max_deduct_per_trip=100000.0)
    )
    check("safety_score", result["safety_score"], 0.0)


def test_score_does_not_exceed_score_base_with_no_events():
    data = [point(event=None, speed=50) for _ in range(5)]
    result = calculate_advanced_trip_score(data, cfg(score_base=100.0))
    check("safety_score", result["safety_score"], 100.0)


# ---------------------------------------------------------------
# max_deduct_per_trip cap
# ---------------------------------------------------------------

def test_max_deduct_per_trip_caps_total_deduction():
    interleaved = []
    for _ in range(3):
        interleaved.append(point(event="speeding", speed=150))
        interleaved.append(point(event=None))
    result = calculate_advanced_trip_score(
        interleaved, cfg(weight_speeding=20.0, max_deduct_per_trip=10.0)
    )
    check("metrics['speeding_count']", result["metrics"]["speeding_count"], 3)
    check_approx("safety_score", result["safety_score"], 100.0 - 10.0)


# ---------------------------------------------------------------
# Night danger zone multiplier (00:00-04:00)
# ---------------------------------------------------------------

def test_night_multiplier_increases_penalty():
    night_ts = datetime.datetime(2026, 6, 1, 2, 0, 0)
    data = [point(event="harsh_brake", speed=40, ts=night_ts)]
    result = calculate_advanced_trip_score(
        data, cfg(night_danger_zone_multiplier=2.0)
    )
    check_approx("safety_score", result["safety_score"], 100.0 - (3.0 * 2.0))


def test_day_time_uses_no_multiplier():
    day_ts = datetime.datetime(2026, 6, 1, 14, 0, 0)
    data = [point(event="harsh_brake", speed=40, ts=day_ts)]
    result = calculate_advanced_trip_score(
        data, cfg(night_danger_zone_multiplier=2.0)
    )
    check_approx("safety_score", result["safety_score"], 100.0 - 3.0)


def test_multiplier_boundary_at_4am_not_applied():
    boundary_ts = datetime.datetime(2026, 6, 1, 4, 0, 0)
    data = [point(event="harsh_brake", speed=40, ts=boundary_ts)]
    result = calculate_advanced_trip_score(
        data, cfg(night_danger_zone_multiplier=2.0)
    )
    check_approx("safety_score", result["safety_score"], 100.0 - 3.0)


def test_ts_not_datetime_uses_no_multiplier():
    data = [point(event="harsh_brake", speed=40, ts=1234567890)]
    result = calculate_advanced_trip_score(
        data, cfg(night_danger_zone_multiplier=2.0)
    )
    check_approx("safety_score", result["safety_score"], 100.0 - 3.0)


def test_ts_none_uses_no_multiplier():
    data = [point(event="harsh_brake", speed=40, ts=None)]
    result = calculate_advanced_trip_score(
        data, cfg(night_danger_zone_multiplier=2.0)
    )
    check_approx("safety_score", result["safety_score"], 100.0 - 3.0)


# ---------------------------------------------------------------
# Mountain road exemption (lat 18.5 - 19.5)
# ---------------------------------------------------------------

def test_mountain_road_brake_penalty_halved():
    data = [point(event="harsh_brake", speed=40, lat=19.0)]
    result = calculate_advanced_trip_score(
        data, cfg(enable_mountain_road_exemption=True)
    )
    check("metrics['harsh_brake_count']", result["metrics"]["harsh_brake_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - (3.0 * 0.5))


def test_mountain_road_corner_event_fully_exempt():
    data = [point(event="harsh_cornering", speed=40, lat=19.0)]
    result = calculate_advanced_trip_score(
        data, cfg(enable_mountain_road_exemption=True)
    )
    check("metrics['harsh_corner_count']", result["metrics"]["harsh_corner_count"], 0)
    check_approx("safety_score", result["safety_score"], 100.0)


def test_non_mountain_corner_event_penalised_normally():
    data = [point(event="harsh_cornering", speed=40, lat=13.75)]
    result = calculate_advanced_trip_score(
        data, cfg(enable_mountain_road_exemption=True)
    )
    check("metrics['harsh_corner_count']", result["metrics"]["harsh_corner_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - 2.0)


# ---------------------------------------------------------------
# Low-speed brake exemption (construction zone / accident delay)
# ---------------------------------------------------------------

def test_low_speed_brake_event_fully_exempt():
    data = [point(event="harsh_brake", speed=10.0)]
    result = calculate_advanced_trip_score(
        data,
        cfg(
            enable_construction_zone_exemption=True,
            enable_accident_delay_exemption=False,
        ),
    )
    check("metrics['harsh_brake_count']", result["metrics"]["harsh_brake_count"], 0)
    check_approx("safety_score", result["safety_score"], 100.0)


def test_brake_event_at_exactly_20kmh_not_exempt():
    data = [point(event="harsh_brake", speed=20.0)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['harsh_brake_count']", result["metrics"]["harsh_brake_count"], 1)


# ---------------------------------------------------------------
# FSM debounce
# ---------------------------------------------------------------

def test_consecutive_same_event_counts_once_not_per_sample():
    data = [point(event="harsh_brake", speed=40) for _ in range(5)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['harsh_brake_count']", result["metrics"]["harsh_brake_count"], 1)
    check_approx("safety_score", result["safety_score"], 100.0 - 3.0)


def test_event_re_triggers_after_returning_to_normal():
    data = [
        point(event="harsh_brake", speed=40),
        point(event=None, speed=40),
        point(event="harsh_brake", speed=40),
    ]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['harsh_brake_count']", result["metrics"]["harsh_brake_count"], 2)


# ---------------------------------------------------------------
# max_speed metric
# ---------------------------------------------------------------

def test_max_speed_metric_tracks_highest_value():
    data = [point(speed=30), point(speed=95.5), point(speed=60)]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['max_speed']", result["metrics"]["max_speed"], 95.5)


def test_missing_speed_defaults_to_zero_without_error():
    data = [{"lat": 13.0, "ts": None, "event": None}]
    result = calculate_advanced_trip_score(data, cfg())
    check("metrics['max_speed']", result["metrics"]["max_speed"], 0.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))