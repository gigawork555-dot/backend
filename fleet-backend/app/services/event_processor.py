# app/services/event_processor.py
#
# FDD v1.4 §10.4 Harsh Event Detection Algorithm — Pure Function
#
# [FIX LOG — this revision]
#   [BUG-4 FIX, kept from previous patch]
#     Axis mapping corrected to match FDD §10.4:
#       Harsh Brake        : ax < -0.4G
#       Harsh Acceleration : ax > +0.4G
#       Harsh Cornering    : |ay| > 0.4G
#
#   [BUG-1 FIX, kept from previous patch]
#     _calculate_severity() guard clause fixed from `threshold <= 0`
#     to `threshold == 0`, since harsh_brake threshold is naturally
#     negative (-0.4G) and the old guard zeroed out its severity always.
#
#   [NEW FIX #4 — Harsh Bump added, in the main config-driven pipeline]
#     FDD §10.4 defines 6 event types: harsh_brake, harsh_acceleration,
#     harsh_cornering, speeding, harsh_bump, idling.
#     This file previously only implemented 5 — harsh_bump (az spike)
#     was completely missing even though scoring_config_cache.bump_deduct
#     already exists in the DB schema and is read by score_calculator.py.
#
#     Per FDD §10.4 table, the bump trigger condition is a FIXED constant
#     (az > +3G or az < -3G) — it is NOT one of the Admin-configurable
#     fields in fleet.telematics.scoring.config (§12.3 config field list
#     only has harsh_brake_g / harsh_accel_g / harsh_corner_g /
#     speeding_kmh_over / idle_min_threshold — no bump_g). Only the
#     *weight* (bump_deduct) is configurable, not the trigger threshold.
#     So BUMP_THRESHOLD_G below is intentionally a module constant,
#     with an optional config override left available for testing.
#
#   [FIX #7 — this revision] filter_imu_noise_event() thresholds/coverage
#     corrected to match FDD v1.4 §10.4 exactly.
#
#     This function is kept only for backward-compatibility callers
#     (it is NOT part of EVENT_HANDLERS / process_event() — the real
#     config-driven pipeline used by mqtt_subscriber.py already used
#     the correct -0.4/0.4/0.4/±3.0 thresholds via _detect_harsh_*()
#     and _detect_bump()). Before this fix it independently hardcoded
#     its own, DIFFERENT, out-of-spec thresholds:
#
#         HARSH_ACCEL_THRESHOLD  = 0.3   (FDD §10.4 requires 0.4)
#         HARSH_CORNER_THRESHOLD = 0.5   (FDD §10.4 requires 0.4)
#
#     and never inspected `az` at all — so any caller relying on this
#     helper had no way to detect Harsh Bump, silently missing 1 of
#     the 6 FDD-defined event types.
#
#     Fixed: thresholds aligned to -0.4 / 0.4 / 0.4 (matching
#     HARSH_BRAKE_THRESHOLD/_detect_harsh_accel()/_detect_harsh_cornering())
#     and an `is_bump` key added using the same ±3.0G fixed threshold as
#     _detect_bump() / BUMP_THRESHOLD_G below, so this helper's output
#     now exactly matches the FDD-compliant detection pipeline for all
#     4 IMU-axis event types (brake / accel / corner / bump).
#
#     NOTE: this only changes the *out-of-spec legacy helper* — the
#     production detection pipeline (process_event() -> EVENT_HANDLERS)
#     was already FDD-compliant and is unaffected by this fix.

from typing import Dict

# FDD §10.4: "Harsh Bump: az > +3G หรือ < -3G" — fixed detection constant
BUMP_THRESHOLD_G = 3.0

# az resting baseline is ~1.0G (gravity) when stationary on a flat road.
# The FDD formula in §10.4 computes severity as (|az| - 9.8) / 1G × 100
# when az is expressed in m/s^2. This module works in G units (see
# ay/ax handling elsewhere in this file), so we treat the "at rest"
# baseline as 1.0G and measure deviation from it for severity, while
# still using the raw ±3G trigger for detection as specified.
BUMP_BASELINE_G = 1.0


def filter_imu_noise_event(
    ax: float,
    ay: float,
    az: float
) -> Dict[str, bool]:
    """
    Backward compatibility

    [FIX #7] Axis mapping AND thresholds now match FDD v1.4 §10.4
    exactly (previously accel/corner thresholds were out of spec, and
    bump detection was missing entirely):
      - Harsh Brake        : ax < -0.4G
      - Harsh Acceleration : ax > +0.4G
      - Harsh Cornering    : |ay| > 0.4G
      - Harsh Bump         : az > +3G or az < -3G
    """

    HARSH_BRAKE_THRESHOLD = -0.4
    HARSH_ACCEL_THRESHOLD = 0.4    # [FIX #7] was 0.3 — FDD §10.4 requires 0.4
    HARSH_CORNER_THRESHOLD = 0.4   # [FIX #7] was 0.5 — FDD §10.4 requires 0.4

    ax = ax or 0.0
    ay = ay or 0.0
    az = az if az is not None else 0.0  # [FIX #7] az previously unused entirely

    return {
        "is_harsh_braking": ax < HARSH_BRAKE_THRESHOLD,
        "is_harsh_acceleration": ax > HARSH_ACCEL_THRESHOLD,
        "is_harsh_cornering": abs(ay) > HARSH_CORNER_THRESHOLD,
        # [FIX #7] new — FDD §10.4 Harsh Bump, using the same fixed
        # ±3G constant as _detect_bump() below (BUMP_THRESHOLD_G)
        "is_bump": az > BUMP_THRESHOLD_G or az < -BUMP_THRESHOLD_G,
    }


def _safe_float(value) -> float:
    """
    ป้องกัน None หรือค่าผิดรูปแบบ
    """

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _calculate_severity(
        value: float,
        threshold: float
) -> float:
    """
    normalize severity ให้อยู่ช่วง 0-1

    [BUG-1 FIX] เช็คแค่ threshold == 0 (ป้องกันหารศูนย์เท่านั้น)
    เพื่อให้ threshold ติดลบ (เช่น harsh_brake = -0.4G) ยังคำนวณ
    severity ได้ถูกต้อง แทนที่จะเป็น 0.00 เสมอ
    """

    if threshold == 0:
        return 0.0

    severity = abs(value) / abs(threshold)

    return min(round(severity, 2), 1.0)


def _detect_harsh_brake(
        payload: dict,
        config: dict
):
    """
    FDD v1.4 §10.4: Harsh Brake — ax < -0.4G (เบรคหัก)
    """

    ax = _safe_float(payload.get("ax"))

    threshold = _safe_float(
        config.get("threshold_harsh_brake", -0.4)
    )

    if ax < threshold:

        severity = _calculate_severity(
            ax,
            threshold
        )

        return "harsh_brake", severity

    return "", 0.0


def _detect_harsh_acceleration(
        payload: dict,
        config: dict
):
    """
    FDD v1.4 §10.4: Harsh Acceleration — ax > +0.4G (เร่งกะทันหัน)
    """

    ax = _safe_float(payload.get("ax"))

    threshold = _safe_float(
        config.get("threshold_harsh_accel", 0.4)
    )

    if ax > threshold:

        severity = _calculate_severity(
            ax,
            threshold
        )

        return "harsh_acceleration", severity

    return "", 0.0


def _detect_harsh_cornering(
        payload: dict,
        config: dict
):
    """
    FDD v1.4 §10.4: Harsh Cornering — |ay| > 0.4G (เลี้ยวหักโค้ง)
    """

    ay = _safe_float(payload.get("ay"))

    threshold = _safe_float(
        config.get("threshold_harsh_corner", 0.4)
    )

    if abs(ay) > threshold:

        severity = _calculate_severity(
            abs(ay),
            threshold
        )

        return "harsh_cornering", severity

    return "", 0.0


def _detect_bump(
        payload: dict,
        config: dict
):
    """
    FDD v1.4 §10.4: Harsh Bump — az > +3G หรือ az < -3G (ชนกระแทก)

    [NEW — Fix #4]
    Trigger threshold เป็นค่าคงที่ตาม FDD (ไม่ใช่ Admin-configurable
    field ใน §12.3) แต่ยอมให้ override ผ่าน config["threshold_bump"]
    ได้เพื่อความสะดวกในการทดสอบ — ถ้าไม่ส่งมาจะใช้ BUMP_THRESHOLD_G (3.0)
    """

    az = _safe_float(payload.get("az"))

    threshold = _safe_float(
        config.get("threshold_bump", BUMP_THRESHOLD_G)
    )

    if az > threshold or az < -threshold:

        # severity อิงตามส่วนเกินจาก baseline แรงโน้มถ่วง (1G) ตาม
        # แนวทาง §10.4: (|az| - baseline) / threshold_span × 100
        severity = _calculate_severity(
            abs(az) - BUMP_BASELINE_G,
            threshold - BUMP_BASELINE_G,
        )

        return "bump", severity

    return "", 0.0


def _detect_speeding(
        payload: dict,
        config: dict
):

    speed = _safe_float(
        payload.get("speed")
    )

    threshold = _safe_float(
        config.get(
            "threshold_speed_kmh",
            90
        )
    )

    if speed > threshold:

        severity = _calculate_severity(
            speed,
            threshold
        )

        return "speeding", severity

    return "", 0.0


def _detect_idling(
        payload: dict,
        config: dict
):
    """
    packet-level idling

    speed = 0
    rpm > 0
    ignition = ON
    """

    speed = _safe_float(
        payload.get("speed")
    )

    rpm = _safe_float(
        payload.get("rpm")
    )

    ignition = bool(
        payload.get("ignition")
    )

    if (
            ignition
            and speed < 1
            and rpm > 500
    ):

        return "idling", 1.0

    return "", 0.0


# [FIX #4] เพิ่ม _detect_bump เข้า pipeline — ครบ 6 event ตาม FDD §10.4
# ลำดับใน tuple มีผลต่อ "which event wins" ถ้าหลาย event ตรงเงื่อนไข
# พร้อมกัน (process_event() คืนแค่ event แรกที่เจอ — break ทันที)
# วาง bump ไว้หลัง harsh_brake/accel/corner เพราะ event เหล่านั้น
# specific กว่า (แยกตามแกน ax/ay) ส่วน bump เป็น az/แรงกระแทกแนวดิ่ง
# ซึ่งมักเกิดพร้อมสภาพถนน ไม่ใช่พฤติกรรมการขับ — ให้ priority ต่ำกว่า
EVENT_HANDLERS = (
    _detect_harsh_brake,
    _detect_harsh_acceleration,
    _detect_harsh_cornering,
    _detect_speeding,
    _detect_bump,
    _detect_idling
)


def process_event(
        payload: dict,
        config: dict
) -> dict:
    """
    วิเคราะห์ packet แล้วคืน payload ใหม่

    Pure Function
    """

    new_payload = payload.copy()

    new_payload["event"] = ""
    new_payload["event_severity"] = 0.0

    for handler in EVENT_HANDLERS:

        event_type, severity = handler(
            payload,
            config
        )

        if event_type:

            new_payload["event"] = event_type
            new_payload["event_severity"] = severity

            break

    return new_payload