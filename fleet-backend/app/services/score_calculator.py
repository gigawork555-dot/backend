# app/services/score_calculator.py
#
# FDD v1.4 §12.3 — Configurable Scoring System
#
# [FIX LOG — this revision]
#
#   [Fix #5] max_deduct_per_trip default corrected 100.0 → 50.0
#            FDD §12.3 table: max_deduct_per_trip default = 50.0
#            (ป้องกันคะแนนติดลบ/หักมากเกินไปต่อ 1 เที่ยว)
#
#   [Fix #6] Weight defaults corrected to match FDD §12.3 table exactly:
#              weight_speeding      5.0  → 10.0  (speeding_deduct default)
#              weight_harsh_corner  2.0  →  3.0  (harsh_corner_deduct default)
#              weight_idling        1.0  →  2.0  (idling_deduct default)
#            weight_harsh_brake (3.0) and weight_harsh_accel (3.0) were
#            already correct and are unchanged.
#
#   [Fix #4] Added Harsh Bump scoring — FDD §10.4 defines 6 event types
#            but this function previously only scored 5 (no bump).
#            Added weight_bump (default 4.0 per FDD §12.3 bump_deduct)
#            and bump_penalty/bump_count, following the same FSM
#            debounce pattern as the other harsh events.
#
#   NOTE: exemption flags (enable_traffic_jam_exemption, etc.) and
#   night_danger_zone_multiplier are NOT part of FDD v1.4 — they are
#   pre-existing extensions in this codebase, kept as-is (out of scope
#   for this fix pass). Their *default value* of False here (rather
#   than True) reflects the already-corrected idling-exemption bug —
#   see trip_manager.py for the matching fix.

import datetime
from typing import List, Dict, Any


def calculate_advanced_trip_score(
    telemetry_data: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    คำนวณคะแนนความปลอดภัยรายเที่ยว

    FDD v1.4 §12.3
    - Pure function
    - Event-based scoring
    - Event Count (FSM)
    - No DB
    - No API
    - No Odoo
    """

    if not telemetry_data:
        return {
            "safety_score": config.get("score_base", 100.0),
            "metrics": {}
        }

    # ==========================================================
    # Config — defaults match FDD v1.4 §12.3 table exactly
    # ==========================================================
    score_base = float(
        config.get("score_base", 100.0)
    )

    # [Fix #6] 5.0 → 10.0 (FDD §12.3: speeding_deduct default = 10.0)
    weight_speeding = float(
        config.get("weight_speeding", 10.0)
    )

# [Fix #8] 3.0 → 5.0 (FDD §12.3: harsh_brake_deduct default = 5.0)
    # เดิม fallback ผิดเป็น 3.0 — ตาราง FDD §12.3 ระบุชัดเจนว่า
    # harsh_brake_deduct default = 5.0 (DB column default ถูกต้อง
    # อยู่แล้วที่ 5.0 ใน init.sql — บั๊กนี้อยู่แค่ฝั่ง Python fallback
    # ที่ใช้เมื่อ config ไม่ส่ง key นี้มา)
    weight_harsh_brake = float(
        config.get("weight_harsh_brake", 5.0)
    )

    weight_harsh_accel = float(
        config.get("weight_harsh_accel", 3.0)
    )

    # [Fix #6] 2.0 → 3.0 (FDD §12.3: harsh_corner_deduct default = 3.0)
    weight_harsh_corner = float(
        config.get("weight_harsh_corner", 3.0)
    )

    # [Fix #6] 1.0 → 2.0 (FDD §12.3: idling_deduct default = 2.0)
    weight_idling = float(
        config.get("weight_idling", 2.0)
    )

    # [Fix #4] new — FDD §12.3: bump_deduct default = 4.0
    weight_bump = float(
        config.get("weight_bump", 4.0)
    )

    idle_min_threshold = float(
        config.get("idle_min_threshold", 5.0)
    )

    # [Fix #5] 100.0 → 50.0 (FDD §12.3: max_deduct_per_trip default = 50.0)
    max_deduct_per_trip = float(
        config.get("max_deduct_per_trip", 50.0)
    )

    night_multiplier = float(
        config.get(
            "night_danger_zone_multiplier",
            1.5
        )
    )

    # ==========================================================
    # Metrics
    # ==========================================================
    speeding_count = 0
    harsh_brake_count = 0
    harsh_accel_count = 0
    harsh_corner_count = 0
    bump_count = 0  # [Fix #4]

    max_speed = 0.0

    # ==========================================================
    # Penalties
    # ==========================================================
    speeding_penalty = 0.0
    brake_penalty = 0.0
    accel_penalty = 0.0
    corner_penalty = 0.0
    idle_penalty = 0.0
    bump_penalty = 0.0  # [Fix #4]

    # ==========================================================
    # FSM State
    # ==========================================================
    in_speeding_event = False
    in_brake_event = False
    in_accel_event = False
    in_corner_event = False
    in_bump_event = False  # [Fix #4]

    # ==========================================================
    # Idle Duration
    # ==========================================================
    idle_start_ts = None
    total_idle_seconds = 0.0

    # ==========================================================
    # Main Loop
    # ==========================================================
    for point in telemetry_data:

        speed = float(
            point.get("speed") or 0.0
        )

        lat = float(
            point.get("lat") or 0.0
        )

        ts = point.get("ts")

        event = point.get("event")

        # ------------------------------------------------------
        # max speed
        # ------------------------------------------------------
        if speed > max_speed:
            max_speed = speed

        # ------------------------------------------------------
        # multiplier
        # ------------------------------------------------------
        multiplier = 1.0

        if (
            ts
            and isinstance(
                ts,
                datetime.datetime
            )
        ):
            if 0 <= ts.hour < 4:
                multiplier = night_multiplier

        # ======================================================
        # Speeding Event
        # ======================================================
        is_speeding = (
            event == "speeding"
        )

        if (
            is_speeding
            and not in_speeding_event
        ):
            speeding_count += 1

            speeding_penalty += (
                weight_speeding
                * multiplier
            )

        in_speeding_event = is_speeding

        # ======================================================
        # Harsh Brake Event
        # ======================================================
        is_brake = (
            event == "harsh_brake"
        )

        if (
            is_brake
            and not in_brake_event
        ):

            is_exempt_low_speed = (
                (
                    config.get(
                        "enable_construction_zone_exemption",
                        True
                    )
                    or
                    config.get(
                        "enable_accident_delay_exemption",
                        True
                    )
                )
                and speed < 20.0
            )

            is_mountain = (
                config.get(
                    "enable_mountain_road_exemption",
                    True
                )
                and 18.5 < lat < 19.5
            )

            if is_exempt_low_speed:
                pass

            elif is_mountain:

                harsh_brake_count += 1

                brake_penalty += (
                    weight_harsh_brake
                    * 0.5
                    * multiplier
                )

            else:

                harsh_brake_count += 1

                brake_penalty += (
                    weight_harsh_brake
                    * multiplier
                )

        in_brake_event = is_brake

        # ======================================================
        # Harsh Acceleration Event
        # ======================================================
        is_accel = (
            event == "harsh_acceleration"
        )

        if (
            is_accel
            and not in_accel_event
        ):

            harsh_accel_count += 1

            accel_penalty += (
                weight_harsh_accel
                * multiplier
            )

        in_accel_event = is_accel

        # ======================================================
        # Harsh Corner Event
        # ======================================================
        is_corner = (
            event == "harsh_cornering"
        )

        if (
            is_corner
            and not in_corner_event
        ):

            is_mountain = (
                config.get(
                    "enable_mountain_road_exemption",
                    True
                )
                and 18.5 < lat < 19.5
            )

            if not is_mountain:

                harsh_corner_count += 1

                corner_penalty += (
                    weight_harsh_corner
                    * multiplier
                )

        in_corner_event = is_corner

        # ======================================================
        # Harsh Bump Event  [Fix #4 — new, FDD §10.4]
        # ======================================================
        is_bump = (
            event == "bump"
        )

        if (
            is_bump
            and not in_bump_event
        ):

            bump_count += 1

            bump_penalty += (
                weight_bump
                * multiplier
            )

        in_bump_event = is_bump

        # ======================================================
        # Engine Idling
        # ======================================================
        ignition = (
            point.get("ignition")
            is True
        )

        is_idle = (
            ignition
            and speed == 0.0
        )

        if (
            is_idle
            and idle_start_ts is None
        ):
            idle_start_ts = ts

        elif (
            not is_idle
            and idle_start_ts is not None
        ):

            if (
                isinstance(
                    ts,
                    datetime.datetime
                )
                and isinstance(
                    idle_start_ts,
                    datetime.datetime
                )
            ):

                duration = (
                    ts - idle_start_ts
                ).total_seconds()

                if duration > 0:
                    total_idle_seconds += duration

            idle_start_ts = None

    # ==========================================================
    # close last idle segment
    # ==========================================================
    if (
        idle_start_ts is not None
    ):

        last_ts = telemetry_data[-1].get("ts")

        if (
            isinstance(
                last_ts,
                datetime.datetime
            )
        ):

            duration = (
                last_ts - idle_start_ts
            ).total_seconds()

            if duration > 0:
                total_idle_seconds += duration

    engine_idle_minutes = (
        total_idle_seconds / 60.0
    )

    # ==========================================================
    # idling penalty
    #
    # [Fix #3 — related] Defaults here are already False (not True),
    # which was the correct fix for the "idling exemption or all-True"
    # bug. The remaining half of that bug lived in trip_manager.py,
    # which was overriding these with hardcoded True regardless of
    # config — see trip_manager.py fix in this same patch set.
    # ==========================================================
    all_exempt = (
        config.get(
            "enable_traffic_jam_exemption",
            False
        )
        or
        config.get(
            "enable_warehouse_idling_exemption",
            False
        )
        or
        config.get(
            "enable_night_rest_exemption",
            False
        )
    )

    if (
        engine_idle_minutes
        > idle_min_threshold
        and not all_exempt
    ):

        idle_penalty = (
            engine_idle_minutes
            - idle_min_threshold
        ) * weight_idling

    # ==========================================================
    # Total deduction
    # ==========================================================
    total_deduct = (
        speeding_penalty
        + brake_penalty
        + accel_penalty
        + corner_penalty
        + bump_penalty        # [Fix #4]
        + idle_penalty
    )

    total_deduct = min(
        total_deduct,
        max_deduct_per_trip
    )

    final_score = (
        score_base
        - total_deduct
    )

    final_score = max(
        0.0,
        min(
            score_base,
            final_score
        )
    )

    # ==========================================================
    # Metrics
    # ==========================================================
    metrics = {
        "max_speed": round(
            max_speed,
            2
        ),
        "speeding_count": speeding_count,
        "harsh_brake_count": harsh_brake_count,
        "harsh_accel_count": harsh_accel_count,
        "harsh_corner_count": harsh_corner_count,
        "bump_count": bump_count,  # [Fix #4]
        "engine_idle_minutes": round(
            engine_idle_minutes,
            2
        ),
    }

    return {
        "safety_score": round(
            final_score,
            2
        ),
        "metrics": metrics
    }