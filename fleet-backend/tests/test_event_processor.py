# tests/test_event_processor.py
"""
Coverage target (FDD §14.2): event_processor.py >= 90%

[แก้ไข] ทุก assert ถูกแทนที่ด้วย check()/check_is() จาก conftest.py
เพื่อ print ค่า actual/expected จริงก่อนเช็ค — รันด้วย `-v -s` เพื่อดูค่า:

    docker compose run --rm backend pytest tests/test_event_processor.py -v -s

Covers:
- filter_imu_noise_event() backward-compat helper (ax/ay/az axis mapping,
  None-safety on ax/ay)
- _safe_float() with None, valid numeric strings, and malformed strings
- _calculate_severity() zero-threshold guard + clamping to 1.0
- every event handler individually:
    _detect_harsh_brake, _detect_harsh_acceleration,
    _detect_harsh_cornering, _detect_bump, _detect_speeding,
    _detect_idling
- boundary G-force behaviour at exactly the threshold vs just under/over
  (0.4G exactly / 0.39G / 0.41G) for brake, accel, cornering
- process_event() returns the FIRST matching event in EVENT_HANDLERS
  order (brake > accel > corner > speeding > bump > idling) and leaves
  event="" / severity=0.0 when nothing matches
- severity is always normalized into the closed interval [0, 1]
"""

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

from conftest import check, check_is, check_approx, check_range  # noqa: E402

from app.services.event_processor import (  # noqa: E402
    filter_imu_noise_event,
    _safe_float,
    _calculate_severity,
    _detect_harsh_brake,
    _detect_harsh_acceleration,
    _detect_harsh_cornering,
    _detect_bump,
    _detect_speeding,
    _detect_idling,
    process_event,
    EVENT_HANDLERS,
    BUMP_THRESHOLD_G,
)

DEFAULT_CONFIG = {
    "threshold_harsh_brake": -0.4,
    "threshold_harsh_accel": 0.4,
    "threshold_harsh_corner": 0.4,
    "threshold_speed_kmh": 90,
    "threshold_bump": 3.0,
}


def cfg(**overrides):
    merged = dict(DEFAULT_CONFIG)
    merged.update(overrides)
    return merged


# =================================================================
# filter_imu_noise_event() — backward-compat helper
# =================================================================

def test_filter_imu_noise_event_detects_harsh_braking():
    result = filter_imu_noise_event(ax=-0.5, ay=0.0, az=1.0)
    check_is("is_harsh_braking", result["is_harsh_braking"], True)
    check_is("is_harsh_acceleration", result["is_harsh_acceleration"], False)
    check_is("is_harsh_cornering", result["is_harsh_cornering"], False)


def test_filter_imu_noise_event_detects_harsh_acceleration():
    result = filter_imu_noise_event(ax=0.5, ay=0.0, az=1.0)
    check_is("is_harsh_acceleration", result["is_harsh_acceleration"], True)
    check_is("is_harsh_braking", result["is_harsh_braking"], False)


def test_filter_imu_noise_event_detects_harsh_cornering():
    result = filter_imu_noise_event(ax=0.0, ay=0.6, az=1.0)
    check_is("is_harsh_cornering", result["is_harsh_cornering"], True)


def test_filter_imu_noise_event_detects_harsh_cornering_negative_ay():
    result = filter_imu_noise_event(ax=0.0, ay=-0.6, az=1.0)
    check_is("is_harsh_cornering", result["is_harsh_cornering"], True)


def test_filter_imu_noise_event_none_ax_ay_defaults_to_zero():
    result = filter_imu_noise_event(ax=None, ay=None, az=1.0)
    expected = {
        "is_harsh_braking": False,
        "is_harsh_acceleration": False,
        "is_harsh_cornering": False,
    }
    check("filter_imu_noise_event(None,None,1.0)", result, expected)


def test_filter_imu_noise_event_normal_driving_no_flags():
    result = filter_imu_noise_event(ax=0.1, ay=0.1, az=1.0)
    check("any(result.values())", any(result.values()), False)


# =================================================================
# _safe_float()
# =================================================================

def test_safe_float_none_returns_zero():
    check("_safe_float(None)", _safe_float(None), 0.0)


def test_safe_float_valid_numeric_string():
    check("_safe_float('1.5')", _safe_float("1.5"), 1.5)


def test_safe_float_valid_int():
    check("_safe_float(7)", _safe_float(7), 7.0)


def test_safe_float_malformed_string_returns_zero():
    check("_safe_float('not-a-number')", _safe_float("not-a-number"), 0.0)


def test_safe_float_malformed_type_returns_zero():
    check("_safe_float(['1.0'])", _safe_float(["1.0"]), 0.0)


# =================================================================
# _calculate_severity()
# =================================================================

def test_calculate_severity_zero_threshold_returns_zero():
    check("_calculate_severity(5.0, 0)", _calculate_severity(5.0, 0), 0.0)


def test_calculate_severity_negative_threshold_uses_abs():
    severity = _calculate_severity(-0.8, -0.4)
    check("_calculate_severity(-0.8,-0.4)", severity, 1.0)


def test_calculate_severity_clamped_to_one():
    severity = _calculate_severity(10.0, 0.4)
    check("_calculate_severity(10.0,0.4)", severity, 1.0)


def test_calculate_severity_normal_ratio_rounded():
    severity = _calculate_severity(0.2, 0.4)
    check("_calculate_severity(0.2,0.4)", severity, 0.5)


def test_calculate_severity_always_in_unit_interval():
    for value, threshold in [
        (0.0, 0.4), (0.4, 0.4), (0.39, 0.4), (100.0, 0.4), (-5.0, -0.4)
    ]:
        s = _calculate_severity(value, threshold)
        check_range(f"_calculate_severity({value},{threshold})", s, 0.0, 1.0)


# =================================================================
# _detect_harsh_brake() — FDD §10.4: ax < -0.4G
# =================================================================

def test_detect_harsh_brake_triggers_below_threshold():
    event, severity = _detect_harsh_brake({"ax": -0.5}, cfg())
    check("event", event, "harsh_brake")
    print(f"  🔎 severity(>0 expected)      -> actual={severity!r}")
    assert severity > 0


def test_detect_harsh_brake_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_harsh_brake({"ax": -0.4}, cfg())
    check("event", event, "")
    check("severity", severity, 0.0)


def test_detect_harsh_brake_boundary_just_under_threshold_not_triggered():
    event, severity = _detect_harsh_brake({"ax": -0.39}, cfg())
    check("event", event, "")


def test_detect_harsh_brake_boundary_just_over_threshold_triggers():
    event, severity = _detect_harsh_brake({"ax": -0.41}, cfg())
    check("event", event, "harsh_brake")


def test_detect_harsh_brake_missing_ax_no_trigger():
    event, severity = _detect_harsh_brake({}, cfg())
    check("event", event, "")
    check("severity", severity, 0.0)


# =================================================================
# _detect_harsh_acceleration() — FDD §10.4: ax > +0.4G
# =================================================================

def test_detect_harsh_acceleration_triggers_above_threshold():
    event, severity = _detect_harsh_acceleration({"ax": 0.5}, cfg())
    check("event", event, "harsh_acceleration")
    print(f"  🔎 severity(>0 expected)      -> actual={severity!r}")
    assert severity > 0


def test_detect_harsh_acceleration_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_harsh_acceleration({"ax": 0.4}, cfg())
    check("event", event, "")


def test_detect_harsh_acceleration_boundary_just_under_not_triggered():
    event, severity = _detect_harsh_acceleration({"ax": 0.39}, cfg())
    check("event", event, "")


def test_detect_harsh_acceleration_boundary_just_over_triggers():
    event, severity = _detect_harsh_acceleration({"ax": 0.41}, cfg())
    check("event", event, "harsh_acceleration")


# =================================================================
# _detect_harsh_cornering() — FDD §10.4: |ay| > 0.4G
# =================================================================

def test_detect_harsh_cornering_triggers_positive_ay():
    event, severity = _detect_harsh_cornering({"ay": 0.5}, cfg())
    check("event", event, "harsh_cornering")


def test_detect_harsh_cornering_triggers_negative_ay():
    event, severity = _detect_harsh_cornering({"ay": -0.5}, cfg())
    check("event", event, "harsh_cornering")


def test_detect_harsh_cornering_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_harsh_cornering({"ay": 0.4}, cfg())
    check("event", event, "")


def test_detect_harsh_cornering_boundary_just_under_not_triggered():
    event, severity = _detect_harsh_cornering({"ay": 0.39}, cfg())
    check("event", event, "")


def test_detect_harsh_cornering_boundary_just_over_triggers():
    event, severity = _detect_harsh_cornering({"ay": 0.41}, cfg())
    check("event", event, "harsh_cornering")


# =================================================================
# _detect_bump() — FDD §10.4: az > +3G or az < -3G
# =================================================================

def test_detect_bump_triggers_above_positive_threshold():
    event, severity = _detect_bump({"az": 3.5}, cfg())
    check("event", event, "bump")
    check_range("severity", severity, 0.0, 1.0)


def test_detect_bump_triggers_below_negative_threshold():
    event, severity = _detect_bump({"az": -3.5}, cfg())
    check("event", event, "bump")


def test_detect_bump_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_bump({"az": 3.0}, cfg())
    check("event", event, "")


def test_detect_bump_uses_default_module_constant_when_not_configured():
    event, severity = _detect_bump({"az": BUMP_THRESHOLD_G + 0.1}, {})
    check("event", event, "bump")


def test_detect_bump_normal_gravity_no_trigger():
    event, severity = _detect_bump({"az": 1.0}, cfg())
    check("event", event, "")
    check("severity", severity, 0.0)


# =================================================================
# _detect_speeding()
# =================================================================

def test_detect_speeding_triggers_above_threshold():
    event, severity = _detect_speeding({"speed": 110}, cfg())
    check("event", event, "speeding")
    print(f"  🔎 severity(>0 expected)      -> actual={severity!r}")
    assert severity > 0


def test_detect_speeding_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_speeding({"speed": 90}, cfg())
    check("event", event, "")


def test_detect_speeding_below_threshold_not_triggered():
    event, severity = _detect_speeding({"speed": 60}, cfg())
    check("event", event, "")


def test_detect_speeding_uses_default_threshold_when_missing():
    event, severity = _detect_speeding({"speed": 95}, {})
    check("event", event, "speeding")


# =================================================================
# _detect_idling() — ignition ON, speed < 1, rpm > 500
# =================================================================

def test_detect_idling_triggers_when_all_conditions_met():
    event, severity = _detect_idling(
        {"speed": 0.0, "rpm": 800, "ignition": True}, cfg()
    )
    check("event", event, "idling")
    check("severity", severity, 1.0)


def test_detect_idling_no_trigger_when_ignition_off():
    event, severity = _detect_idling(
        {"speed": 0.0, "rpm": 800, "ignition": False}, cfg()
    )
    check("event", event, "")


def test_detect_idling_no_trigger_when_moving():
    event, severity = _detect_idling(
        {"speed": 5.0, "rpm": 800, "ignition": True}, cfg()
    )
    check("event", event, "")


def test_detect_idling_no_trigger_when_rpm_too_low():
    event, severity = _detect_idling(
        {"speed": 0.0, "rpm": 400, "ignition": True}, cfg()
    )
    check("event", event, "")


def test_detect_idling_boundary_speed_just_under_one_triggers():
    event, severity = _detect_idling(
        {"speed": 0.99, "rpm": 600, "ignition": True}, cfg()
    )
    check("event", event, "idling")


def test_detect_idling_boundary_rpm_exactly_500_not_triggered():
    event, severity = _detect_idling(
        {"speed": 0.0, "rpm": 500, "ignition": True}, cfg()
    )
    check("event", event, "")


# =================================================================
# EVENT_HANDLERS pipeline order
# =================================================================

def test_event_handlers_order_matches_fdd_priority():
    expected_order = (
        _detect_harsh_brake,
        _detect_harsh_acceleration,
        _detect_harsh_cornering,
        _detect_speeding,
        _detect_bump,
        _detect_idling,
    )
    actual_names = [h.__name__ for h in EVENT_HANDLERS]
    expected_names = [h.__name__ for h in expected_order]
    check("EVENT_HANDLERS order", actual_names, expected_names)


# =================================================================
# process_event() — returns first match, pure function contract
# =================================================================

def test_process_event_no_match_returns_empty_event():
    payload = {"ax": 0.0, "ay": 0.0, "az": 1.0, "speed": 30, "rpm": 1500, "ignition": True}
    result = process_event(payload, cfg())
    check("result['event']", result["event"], "")
    check("result['event_severity']", result["event_severity"], 0.0)


def test_process_event_brake_wins_over_all_others_when_multiple_match():
    payload = {
        "ax": -0.9,
        "speed": 150,
        "rpm": 3000,
        "ignition": True,
    }
    result = process_event(payload, cfg())
    check("result['event']", result["event"], "harsh_brake")


def test_process_event_speeding_wins_over_bump_and_idling():
    payload = {
        "speed": 150,
        "az": 5.0,
        "rpm": 0,
        "ignition": False,
    }
    result = process_event(payload, cfg())
    check("result['event']", result["event"], "speeding")


def test_process_event_bump_wins_over_idling():
    payload = {
        "az": 5.0,
        "speed": 0.0,
        "rpm": 800,
        "ignition": True,
    }
    result = process_event(payload, cfg())
    check("result['event']", result["event"], "bump")


def test_process_event_idling_returned_when_only_idling_matches():
    payload = {"speed": 0.0, "rpm": 800, "ignition": True, "ax": 0.0, "ay": 0.0, "az": 1.0}
    result = process_event(payload, cfg())
    check("result['event']", result["event"], "idling")
    check("result['event_severity']", result["event_severity"], 1.0)


def test_process_event_does_not_mutate_input_payload():
    payload = {"ax": -0.9, "speed": 30, "rpm": 1000, "ignition": True}
    original = dict(payload)
    process_event(payload, cfg())
    check("payload (unchanged)", payload, original)


def test_process_event_severity_always_in_unit_interval():
    payload = {"ax": -50.0, "speed": 30, "rpm": 1000, "ignition": True}
    result = process_event(payload, cfg())
    check_range("result['event_severity']", result["event_severity"], 0.0, 1.0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))