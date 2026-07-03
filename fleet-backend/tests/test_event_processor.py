# tests/test_event_processor.py
"""
Coverage target (FDD §14.2): event_processor.py >= 90%

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
# ทำให้ `import app.services...` เจอ ไม่ว่าจะรันด้วยวิธีไหน:
#   - pytest tests/                              (pytest จัดการ sys.path ให้เองอยู่แล้ว)
#   - python tests/test_event_processor.py        (จาก repo root)
#   - python test_event_processor.py              (จากภายใน tests/ เอง)
#   - python -m tests.test_event_processor         (จาก repo root)
# โดยการแทรก repo root (parent ของโฟลเดอร์ tests/) เข้า sys.path[0]
# ก่อนบรรทัด import ใดๆ ที่อ้างถึง `app`
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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
    assert result["is_harsh_braking"] is True
    assert result["is_harsh_acceleration"] is False
    assert result["is_harsh_cornering"] is False


def test_filter_imu_noise_event_detects_harsh_acceleration():
    result = filter_imu_noise_event(ax=0.5, ay=0.0, az=1.0)
    assert result["is_harsh_acceleration"] is True
    assert result["is_harsh_braking"] is False


def test_filter_imu_noise_event_detects_harsh_cornering():
    result = filter_imu_noise_event(ax=0.0, ay=0.6, az=1.0)
    assert result["is_harsh_cornering"] is True


def test_filter_imu_noise_event_detects_harsh_cornering_negative_ay():
    result = filter_imu_noise_event(ax=0.0, ay=-0.6, az=1.0)
    assert result["is_harsh_cornering"] is True


def test_filter_imu_noise_event_none_ax_ay_defaults_to_zero():
    # ax/ay None must not raise — coerced to 0.0 via `ax or 0.0`
    result = filter_imu_noise_event(ax=None, ay=None, az=1.0)
    assert result == {
        "is_harsh_braking": False,
        "is_harsh_acceleration": False,
        "is_harsh_cornering": False,
    }


def test_filter_imu_noise_event_normal_driving_no_flags():
    result = filter_imu_noise_event(ax=0.1, ay=0.1, az=1.0)
    assert not any(result.values())


# =================================================================
# _safe_float()
# =================================================================

def test_safe_float_none_returns_zero():
    assert _safe_float(None) == 0.0


def test_safe_float_valid_numeric_string():
    assert _safe_float("1.5") == 1.5


def test_safe_float_valid_int():
    assert _safe_float(7) == 7.0


def test_safe_float_malformed_string_returns_zero():
    assert _safe_float("not-a-number") == 0.0


def test_safe_float_malformed_type_returns_zero():
    assert _safe_float(["1.0"]) == 0.0


# =================================================================
# _calculate_severity()
# =================================================================

def test_calculate_severity_zero_threshold_returns_zero():
    assert _calculate_severity(5.0, 0) == 0.0


def test_calculate_severity_negative_threshold_uses_abs():
    # harsh_brake threshold is naturally negative (-0.4G).
    # raw ratio = |-0.8| / |-0.4| = 2.0, but result must clamp to 1.0
    severity = _calculate_severity(-0.8, -0.4)
    assert severity == 1.0


def test_calculate_severity_clamped_to_one():
    severity = _calculate_severity(10.0, 0.4)
    assert severity == 1.0


def test_calculate_severity_normal_ratio_rounded():
    severity = _calculate_severity(0.2, 0.4)
    assert severity == 0.5


def test_calculate_severity_always_in_unit_interval():
    for value, threshold in [
        (0.0, 0.4), (0.4, 0.4), (0.39, 0.4), (100.0, 0.4), (-5.0, -0.4)
    ]:
        s = _calculate_severity(value, threshold)
        assert 0.0 <= s <= 1.0


# =================================================================
# _detect_harsh_brake() — FDD §10.4: ax < -0.4G
# =================================================================

def test_detect_harsh_brake_triggers_below_threshold():
    event, severity = _detect_harsh_brake({"ax": -0.5}, cfg())
    assert event == "harsh_brake"
    assert severity > 0


def test_detect_harsh_brake_boundary_exactly_at_threshold_not_triggered():
    # condition is strictly `ax < threshold`; ax == -0.4 must NOT trigger
    event, severity = _detect_harsh_brake({"ax": -0.4}, cfg())
    assert event == ""
    assert severity == 0.0


def test_detect_harsh_brake_boundary_just_under_threshold_not_triggered():
    # -0.39 is less severe than -0.4 -> should not trigger
    event, severity = _detect_harsh_brake({"ax": -0.39}, cfg())
    assert event == ""


def test_detect_harsh_brake_boundary_just_over_threshold_triggers():
    # -0.41 is more negative than -0.4 -> triggers
    event, severity = _detect_harsh_brake({"ax": -0.41}, cfg())
    assert event == "harsh_brake"


def test_detect_harsh_brake_missing_ax_no_trigger():
    event, severity = _detect_harsh_brake({}, cfg())
    assert event == ""
    assert severity == 0.0


# =================================================================
# _detect_harsh_acceleration() — FDD §10.4: ax > +0.4G
# =================================================================

def test_detect_harsh_acceleration_triggers_above_threshold():
    event, severity = _detect_harsh_acceleration({"ax": 0.5}, cfg())
    assert event == "harsh_acceleration"
    assert severity > 0


def test_detect_harsh_acceleration_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_harsh_acceleration({"ax": 0.4}, cfg())
    assert event == ""


def test_detect_harsh_acceleration_boundary_just_under_not_triggered():
    event, severity = _detect_harsh_acceleration({"ax": 0.39}, cfg())
    assert event == ""


def test_detect_harsh_acceleration_boundary_just_over_triggers():
    event, severity = _detect_harsh_acceleration({"ax": 0.41}, cfg())
    assert event == "harsh_acceleration"


# =================================================================
# _detect_harsh_cornering() — FDD §10.4: |ay| > 0.4G
# =================================================================

def test_detect_harsh_cornering_triggers_positive_ay():
    event, severity = _detect_harsh_cornering({"ay": 0.5}, cfg())
    assert event == "harsh_cornering"


def test_detect_harsh_cornering_triggers_negative_ay():
    event, severity = _detect_harsh_cornering({"ay": -0.5}, cfg())
    assert event == "harsh_cornering"


def test_detect_harsh_cornering_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_harsh_cornering({"ay": 0.4}, cfg())
    assert event == ""


def test_detect_harsh_cornering_boundary_just_under_not_triggered():
    event, severity = _detect_harsh_cornering({"ay": 0.39}, cfg())
    assert event == ""


def test_detect_harsh_cornering_boundary_just_over_triggers():
    event, severity = _detect_harsh_cornering({"ay": 0.41}, cfg())
    assert event == "harsh_cornering"


# =================================================================
# _detect_bump() — FDD §10.4: az > +3G or az < -3G
# =================================================================

def test_detect_bump_triggers_above_positive_threshold():
    event, severity = _detect_bump({"az": 3.5}, cfg())
    assert event == "bump"
    assert 0.0 <= severity <= 1.0


def test_detect_bump_triggers_below_negative_threshold():
    event, severity = _detect_bump({"az": -3.5}, cfg())
    assert event == "bump"


def test_detect_bump_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_bump({"az": 3.0}, cfg())
    assert event == ""


def test_detect_bump_uses_default_module_constant_when_not_configured():
    # config without "threshold_bump" key -> falls back to BUMP_THRESHOLD_G
    event, severity = _detect_bump({"az": BUMP_THRESHOLD_G + 0.1}, {})
    assert event == "bump"


def test_detect_bump_normal_gravity_no_trigger():
    event, severity = _detect_bump({"az": 1.0}, cfg())
    assert event == ""
    assert severity == 0.0


# =================================================================
# _detect_speeding()
# =================================================================

def test_detect_speeding_triggers_above_threshold():
    event, severity = _detect_speeding({"speed": 110}, cfg())
    assert event == "speeding"
    assert severity > 0


def test_detect_speeding_boundary_exactly_at_threshold_not_triggered():
    event, severity = _detect_speeding({"speed": 90}, cfg())
    assert event == ""


def test_detect_speeding_below_threshold_not_triggered():
    event, severity = _detect_speeding({"speed": 60}, cfg())
    assert event == ""


def test_detect_speeding_uses_default_threshold_when_missing():
    event, severity = _detect_speeding({"speed": 95}, {})
    assert event == "speeding"


# =================================================================
# _detect_idling() — ignition ON, speed < 1, rpm > 500
# =================================================================

def test_detect_idling_triggers_when_all_conditions_met():
    event, severity = _detect_idling(
        {"speed": 0.0, "rpm": 800, "ignition": True}, cfg()
    )
    assert event == "idling"
    assert severity == 1.0


def test_detect_idling_no_trigger_when_ignition_off():
    event, severity = _detect_idling(
        {"speed": 0.0, "rpm": 800, "ignition": False}, cfg()
    )
    assert event == ""


def test_detect_idling_no_trigger_when_moving():
    event, severity = _detect_idling(
        {"speed": 5.0, "rpm": 800, "ignition": True}, cfg()
    )
    assert event == ""


def test_detect_idling_no_trigger_when_rpm_too_low():
    event, severity = _detect_idling(
        {"speed": 0.0, "rpm": 400, "ignition": True}, cfg()
    )
    assert event == ""


def test_detect_idling_boundary_speed_just_under_one_triggers():
    event, severity = _detect_idling(
        {"speed": 0.99, "rpm": 600, "ignition": True}, cfg()
    )
    assert event == "idling"


def test_detect_idling_boundary_rpm_exactly_500_not_triggered():
    # condition is strictly `rpm > 500`
    event, severity = _detect_idling(
        {"speed": 0.0, "rpm": 500, "ignition": True}, cfg()
    )
    assert event == ""


# =================================================================
# EVENT_HANDLERS pipeline order
# =================================================================

def test_event_handlers_order_matches_fdd_priority():
    assert EVENT_HANDLERS == (
        _detect_harsh_brake,
        _detect_harsh_acceleration,
        _detect_harsh_cornering,
        _detect_speeding,
        _detect_bump,
        _detect_idling,
    )


# =================================================================
# process_event() — returns first match, pure function contract
# =================================================================

def test_process_event_no_match_returns_empty_event():
    payload = {"ax": 0.0, "ay": 0.0, "az": 1.0, "speed": 30, "rpm": 1500, "ignition": True}
    result = process_event(payload, cfg())
    assert result["event"] == ""
    assert result["event_severity"] == 0.0


def test_process_event_brake_wins_over_all_others_when_multiple_match():
    # ax triggers both brake AND accel-shaped severity checks would be
    # impossible simultaneously (ax can't be both < -0.4 and > 0.4), so
    # to prove "brake wins first" we make brake AND speeding both true —
    # brake must be returned since it's earlier in EVENT_HANDLERS.
    payload = {
        "ax": -0.9,        # triggers harsh_brake
        "speed": 150,       # would also trigger speeding
        "rpm": 3000,
        "ignition": True,
    }
    result = process_event(payload, cfg())
    assert result["event"] == "harsh_brake"


def test_process_event_speeding_wins_over_bump_and_idling():
    payload = {
        "speed": 150,   # triggers speeding
        "az": 5.0,      # would also trigger bump
        "rpm": 0,
        "ignition": False,
    }
    result = process_event(payload, cfg())
    assert result["event"] == "speeding"


def test_process_event_bump_wins_over_idling():
    payload = {
        "az": 5.0,       # triggers bump
        "speed": 0.0,    # would also trigger idling
        "rpm": 800,
        "ignition": True,
    }
    result = process_event(payload, cfg())
    assert result["event"] == "bump"


def test_process_event_idling_returned_when_only_idling_matches():
    payload = {"speed": 0.0, "rpm": 800, "ignition": True, "ax": 0.0, "ay": 0.0, "az": 1.0}
    result = process_event(payload, cfg())
    assert result["event"] == "idling"
    assert result["event_severity"] == 1.0


def test_process_event_does_not_mutate_input_payload():
    payload = {"ax": -0.9, "speed": 30, "rpm": 1000, "ignition": True}
    original = dict(payload)
    process_event(payload, cfg())
    assert payload == original


def test_process_event_severity_always_in_unit_interval():
    payload = {"ax": -50.0, "speed": 30, "rpm": 1000, "ignition": True}
    result = process_event(payload, cfg())
    assert 0.0 <= result["event_severity"] <= 1.0


# =================================================================
# Standalone runner — เปิดทางให้เรียก `python tests/test_event_processor.py`
# ได้โดยตรง โดยยังใช้ pytest engine เต็มรูปแบบ (ไม่ใช่ manual test loop)
#
# ปกติ pytest จะ auto-discover ไฟล์ tests/*.py เอง — บล็อกนี้มีไว้แค่
# กรณีอยากรันไฟล์เดียวเร็วๆ ตอน dev โดยไม่พิมพ์ `pytest tests/test_event_processor.py`
# =================================================================
if __name__ == "__main__":
    import sys
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))
