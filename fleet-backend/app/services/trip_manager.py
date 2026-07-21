# app/services/trip_manager.py

"""
Trip Manager

Responsibilities:
- ตรวจจับ Trip Start / Trip End จาก ignition flag ใน telemetry_raw
- Debounce 30 วินาทีก่อน finalize เพื่อป้องกัน false trip cut
- คำนวณ Driver Score ผ่าน score_calculator
- INSERT ลง trip_logs
- รองรับหลายรถพร้อมกัน ด้วย per-device Lock และ State แยกกัน

FDD v1.4 Compliant

[FIX LOG — this revision]

  [Fix #3] get_active_scoring_config(): idling exemption flags
           (enable_traffic_jam_exemption, enable_warehouse_idling_exemption,
           enable_night_rest_exemption) were hardcoded to True in BOTH the
           DB-driven return path and the no-config fallback path.

           Effect: score_calculator.calculate_advanced_trip_score()'s
           `all_exempt = flag1 or flag2 or flag3` was ALWAYS True
           regardless of what score_calculator's own (already-fixed)
           defaults said — because this function overrode them with
           explicit True on every call. Idling penalty was silently
           never applied in the real running system.

           FDD v1.4 has no concept of these exemptions at all (they are
           an out-of-scope extension in this codebase — see separate
           tracking item). Since they're not Admin-configurable per the
           FDD §12.3 config model, the correct default here is False,
           matching score_calculator.py's corrected default, so idling
           penalties actually apply as specified in FDD §12.3.

  [Fix #4 support] Added "weight_bump" mapping (DB column bump_deduct)
           so score_calculator's new Harsh Bump penalty (FDD §10.4/§12.3)
           actually receives its configured weight instead of always
           falling back to the hardcoded default.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app.services.score_calculator import calculate_advanced_trip_score

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

DEBOUNCE_SECONDS: int = 30
MIN_TRIP_POINTS:  int = 2


# ──────────────────────────────────────────────────────────────
# Per-device State
# ──────────────────────────────────────────────────────────────

@dataclass
class TripState:
    is_running:              bool                    = False
    start_time:              Optional[datetime]      = None
    last_ignition_off_time:  Optional[datetime]      = None


# Global dictionaries — keyed by device_id
# แต่ละรถมี State, Lock, และ debounce Task ของตัวเองแยกกัน
TRIP_STATE:      dict[str, TripState]       = {}
DEVICE_LOCKS:    dict[str, asyncio.Lock]    = {}
TRIP_END_TASKS:  dict[str, asyncio.Task]    = {}


def _get_state(device_id: str) -> TripState:
    if device_id not in TRIP_STATE:
        TRIP_STATE[device_id] = TripState()
    return TRIP_STATE[device_id]


def _get_lock(device_id: str) -> asyncio.Lock:
    if device_id not in DEVICE_LOCKS:
        DEVICE_LOCKS[device_id] = asyncio.Lock()
    return DEVICE_LOCKS[device_id]


# ──────────────────────────────────────────────────────────────
# get_active_scoring_config
#
# ดึงเกณฑ์คำนวณคะแนนล่าสุดจาก scoring_config_cache และ map ชื่อ
# column DB → key ที่ calculate_advanced_trip_score() อ่าน
# ──────────────────────────────────────────────────────────────

async def get_active_scoring_config(
    connection: asyncpg.Connection
) -> dict:
    """
    ดึงเกณฑ์คำนวณคะแนนล่าสุดจาก scoring_config_cache

    Key ที่คืนออกมาตรงกับที่ calculate_advanced_trip_score() อ่าน
    ทุกตัว รวมถึง weight_bump (Fix #4) และ exemption flags ที่แก้เป็น
    False แล้ว (Fix #3)
    """

    query = """
        SELECT *
        FROM scoring_config_cache
        WHERE is_active = TRUE
        LIMIT 1;
    """

    row = await connection.fetchrow(query)

    if row:

        raw = dict(row)

        return {

            # ── Base ──────────────────────────────────────────
            "score_base":
                float(raw.get("score_base",           100.0)),

            # ── Deduction weights (FDD §12.3 defaults) ─────────
            "weight_speeding":
                float(raw.get("speeding_deduct",       10.0)),

            # [Fix #8] 3.0 → 5.0 (FDD §12.3: harsh_brake_deduct default = 5.0)
            "weight_harsh_brake":
                float(raw.get("harsh_brake_deduct",     5.0)),

            "weight_harsh_accel":
                float(raw.get("harsh_accel_deduct",     3.0)),

            "weight_harsh_corner":
                float(raw.get("harsh_corner_deduct",    3.0)),

            "weight_idling":
                float(raw.get("idling_deduct",          2.0)),

            # [Fix #4 support] bump_deduct → weight_bump
            "weight_bump":
                float(raw.get("bump_deduct",            4.0)),

            # ── Detection thresholds ──────────────────────────
            "speeding_kmh_over":
                float(raw.get("speeding_kmh_over",     20.0)),

            "idle_min_threshold":
                float(raw.get("idle_min_threshold",     5.0)),

            # DB stores harsh_brake_g as a positive magnitude (e.g. 0.40);
            # the brake threshold convention elsewhere is negative
            # (ax < -0.4G), so flip sign here.
            "threshold_harsh_brake":
                -abs(float(raw["harsh_brake_g"]))
                if raw.get("harsh_brake_g") is not None
                else -0.4,

            "threshold_harsh_accel":
                float(raw.get("harsh_accel_g",          0.4)),

            "threshold_harsh_corner":
                float(raw.get("harsh_corner_g",         0.4)),

            # ── Trip cap ──────────────────────────────────────
            # [Fix #5] fallback default corrected 100.0 → 50.0
            "max_deduct_per_trip":
                float(raw.get("max_deduct_per_trip",   50.0)),

            # ── Advanced features (out of FDD scope, unchanged) ─
            "night_danger_zone_multiplier":        1.5,
            "enable_construction_zone_exemption":  True,
            "enable_accident_delay_exemption":     True,
            "enable_mountain_road_exemption":      True,

            # [Fix #3] hardcoded True → False so idling penalty
            # actually applies per FDD §12.3 (these 3 flags no longer
            # unconditionally exempt every trip's idling time)
            "enable_traffic_jam_exemption":        False,
            "enable_warehouse_idling_exemption":   False,
            "enable_night_rest_exemption":         False,
        }

    # ── Fallback ถ้า DB ไม่มี active config ──────────────────
    logger.warning(
        "No active scoring config found in DB — using FDD v1.4 §12.3 defaults"
    )

    return {
        "score_base":                           100.0,
        # [Fix #6] defaults corrected to match FDD §12.3 table
        "weight_speeding":                       10.0,
        "weight_harsh_brake":                     5.0,   # [Fix #8] FDD §12.3
        "weight_harsh_accel":                     3.0,
        "weight_harsh_corner":                    3.0,
        "weight_idling":                          2.0,
        "weight_bump":                            4.0,   # [Fix #4]
        "speeding_kmh_over":                     20.0,
        "idle_min_threshold":                     5.0,
        "threshold_harsh_brake":                 -0.4,
        "threshold_harsh_accel":                  0.4,
        "threshold_harsh_corner":                 0.4,
        # [Fix #5] 100.0 → 50.0
        "max_deduct_per_trip":                   50.0,
        "night_danger_zone_multiplier":           1.5,
        "enable_construction_zone_exemption":    True,
        "enable_accident_delay_exemption":       True,
        "enable_mountain_road_exemption":        True,
        # [Fix #3] True → False
        "enable_traffic_jam_exemption":          False,
        "enable_warehouse_idling_exemption":     False,
        "enable_night_rest_exemption":           False,
    }


# ──────────────────────────────────────────────────────────────
# Haversine distance
# ──────────────────────────────────────────────────────────────

def _haversine_km(
    lat1: float, lon1: float,
    lat2: float, lon2: float
) -> float:

    R = 6371.0

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi       = math.radians(lat2 - lat1)
    dlambda    = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2)
        * math.sin(dlambda / 2) ** 2
    )

    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ──────────────────────────────────────────────────────────────
# Fuel estimation (MAF-based → fallback distance-based)
# ──────────────────────────────────────────────────────────────

def _estimate_fuel(
    telemetry_points: list[dict],
    distance_km:      float
) -> float:

    maf_points = [
        p for p in telemetry_points
        if p.get("maf_airflow") is not None
        and float(p.get("maf_airflow", 0)) > 0
    ]

    if maf_points:
        avg_maf  = sum(float(p["maf_airflow"]) for p in maf_points) / len(maf_points)
        duration = len(telemetry_points) * 5 / 3600.0   # assume 5s per point → hours
        return round(avg_maf * duration / 14.7 * 0.72 / 1000, 2)

    # fallback: 10L/100km
    return round(distance_km * 0.10, 2)


# ──────────────────────────────────────────────────────────────
# GPS track builder (ส่ง array จุดพิกัดให้ Odoo ใช้วาดแผนที่)
# ──────────────────────────────────────────────────────────────

def _build_gps_track(
    telemetry_points: list[dict]
) -> list[dict]:

    track = []
    prev_lat = prev_lon = None

    for p in telemetry_points:

        lat = p.get("lat")
        lon = p.get("lon")

        if lat is None or lon is None:
            continue

        # Skip duplicate coordinates (GPS jitter)
        if lat == prev_lat and lon == prev_lon:
            continue

        track.append({
            "ts":    str(p.get("ts", "")),
            "lat":   lat,
            "lon":   lon,
            "speed": p.get("speed", 0),
        })

        prev_lat, prev_lon = lat, lon

    return track


# ──────────────────────────────────────────────────────────────
# Finalize Trip — INSERT to trip_logs
# ──────────────────────────────────────────────────────────────

async def _finalize_trip(
    pool:       asyncpg.Pool,
    device_id:  str,
    start_time: datetime,
    end_time:   datetime,
) -> None:
    """
    เรียกหลัง debounce ครบ 30 วินาที
    - query telemetry_raw ช่วง [start_time, end_time]
    - คำนวณ metrics และ driver_score
    - INSERT ลง trip_logs
    """

    async with pool.acquire() as connection:

        # ── 1. ดึง telemetry points ──────────────────────────
        rows = await connection.fetch(
            """
            SELECT
                ts, lat, lon, speed, heading,
                rpm, throttle, engine_load, fuel_level,
                maf_airflow,
                ax, ay, az, gx, gy, gz,
                event, event_severity, ignition
            FROM telemetry_raw
            WHERE device_id = $1
              AND ts BETWEEN $2 AND $3
            ORDER BY ts ASC
            """,
            device_id,
            start_time,
            end_time,
        )

        telemetry_points = [dict(r) for r in rows]

        logger.info(
            f"[TripManager] {device_id} "
            f"points={len(telemetry_points)}"
        )

        if len(telemetry_points) < MIN_TRIP_POINTS:
            logger.warning(
                f"[TripManager] {device_id} "
                f"too few points ({len(telemetry_points)}) — skip"
            )
            return

        # ── 2. Load scoring config (Fix #3 / #4 / #5 / #6 applied) ─
        config = await get_active_scoring_config(connection)

        # ── 3. คำนวณ Driver Score ─────────────────────────────
        result  = calculate_advanced_trip_score(telemetry_points, config)
        metrics = result["metrics"]

        # ── 4. duration ───────────────────────────────────────
        duration_min = (
            end_time - start_time
        ).total_seconds() / 60.0

        # ── 5. distance (haversine) ───────────────────────────
        distance_km = 0.0
        valid = [
            p for p in telemetry_points
            if p.get("lat") and p.get("lon")
        ]

        for i in range(1, len(valid)):
            distance_km += _haversine_km(
                valid[i - 1]["lat"], valid[i - 1]["lon"],
                valid[i]["lat"],     valid[i]["lon"],
            )

        distance_km = round(distance_km, 3)

        # ── 6. average speed ──────────────────────────────────
        speeds     = [float(p["speed"]) for p in telemetry_points if p.get("speed") is not None]
        avg_speed  = round(sum(speeds) / len(speeds), 1) if speeds else 0.0
        max_speed  = round(max(speeds), 1) if speeds else 0.0

        # ── 7. idle time ──────────────────────────────────────
        idle_min = round(metrics.get("engine_idle_minutes", 0.0), 2)

        # ── 8. event counts ───────────────────────────────────
        harsh_brake_count  = metrics.get("harsh_brake_count",  0)
        harsh_accel_count  = metrics.get("harsh_accel_count",  0)
        harsh_corner_count = metrics.get("harsh_corner_count", 0)
        speeding_count     = metrics.get("speeding_count",     0)
        # bump_count is available in metrics (Fix #4) but trip_logs
        # has no dedicated bump_count column in the current schema —
        # its penalty is already folded into driver_score below.

        # ── 9. fuel estimate ──────────────────────────────────
        fuel_used = _estimate_fuel(telemetry_points, distance_km)

        # ── 10. GPS track ─────────────────────────────────────
        import json
        gps_track = json.dumps(_build_gps_track(telemetry_points))

        # ── 11. vehicle_id / driver_id from devices ───────────
        device_row = await connection.fetchrow(
            """
            SELECT d.vehicle_id, us.driver_id
            FROM devices d
            LEFT JOIN update_status us ON us.device_id = d.id
            WHERE d.id = $1
            LIMIT 1
            """,
            device_id,
        )
        vehicle_id = device_row["vehicle_id"] if device_row else None
        driver_id  = device_row["driver_id"]  if device_row else None

        # ── 12. INSERT trip_logs ──────────────────────────────
        await connection.execute(
            """
            INSERT INTO trip_logs (
                device_id, vehicle_id, driver_id,
                trip_start, trip_end,
                distance_km, duration_min, idle_min,
                max_speed, avg_speed,
                harsh_brake_count, harsh_accel_count,
                harsh_corner_count, speeding_count,
                driver_score, fuel_used,
                gps_track, synced_to_odoo, created_at
            ) VALUES (
                $1,  $2,  $3,
                $4,  $5,
                $6,  $7,  $8,
                $9,  $10,
                $11, $12,
                $13, $14,
                $15, $16,
                $17::jsonb, FALSE, NOW()
            )
            """,
            device_id,  vehicle_id,  driver_id,
            start_time, end_time,
            distance_km, round(duration_min, 2), idle_min,
            max_speed,   avg_speed,
            harsh_brake_count,  harsh_accel_count,
            harsh_corner_count, speeding_count,
            round(result["safety_score"], 2), fuel_used,
            gps_track,
        )

        logger.info(
            f"[TripManager] {device_id} trip saved "
            f"score={result['safety_score']:.1f} "
            f"dist={distance_km:.1f}km "
            f"dur={duration_min:.1f}min "
            f"bump_count={metrics.get('bump_count', 0)}"
        )


# ──────────────────────────────────────────────────────────────
# Debounce Task — รอ 30 วินาทีก่อน finalize
# ──────────────────────────────────────────────────────────────

async def _debounce_and_finalize(
    pool:       asyncpg.Pool,
    device_id:  str,
    start_time: datetime,
    end_time:   datetime,
) -> None:

    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)

        async with _get_lock(device_id):

            state = _get_state(device_id)

            # ถ้า ignition กลับมา ON ระหว่างรอ → ยกเลิก finalize
            if state.is_running and state.last_ignition_off_time is None:
                logger.info(
                    f"[TripManager] {device_id} "
                    f"ignition resumed — debounce cancelled"
                )
                return

            # Finalize
            state.is_running             = False
            state.start_time             = None
            state.last_ignition_off_time = None
            TRIP_END_TASKS.pop(device_id, None)

        await _finalize_trip(pool, device_id, start_time, end_time)

    except asyncio.CancelledError:
        logger.info(
            f"[TripManager] {device_id} debounce cancelled"
        )


# ──────────────────────────────────────────────────────────────
# Main Entry Point — เรียกจาก mqtt_subscriber ทุก message
# ──────────────────────────────────────────────────────────────

async def handle_telemetry(
    pool:    asyncpg.Pool,
    payload: dict,
) -> None:
    """
    ประมวลผล telemetry 1 message จาก MQTT

    ตรวจ ignition flag:
    - ON  → เริ่ม trip ถ้ายังไม่มี / ยกเลิก debounce ถ้ากำลังนับอยู่
    - OFF → เริ่ม debounce 30 วินาที
    """

    device_id = payload.get("device_id")
    ignition  = payload.get("ignition", True)

    if not device_id:
        return

    async with _get_lock(device_id):

        state = _get_state(device_id)

        # ── CASE 1: ignition ON ───────────────────────────────
        if ignition:

            # ยกเลิก debounce task ถ้ากำลังนับอยู่
            existing_task = TRIP_END_TASKS.pop(device_id, None)

            if existing_task:
                existing_task.cancel()
                state.last_ignition_off_time = None
                logger.info(
                    f"[TripManager] {device_id} "
                    f"ignition ON — debounce cancelled, trip continues"
                )

            # เริ่ม trip ใหม่ถ้ายังไม่มี
            if not state.is_running:
                state.is_running  = True
                # payload["ts"] อาจเป็น int/float (unix epoch) หรือ datetime
                # _finalize_trip ต้องการ datetime → แปลงให้ถูกต้อง
                _raw_start = payload.get("ts", None)
                if isinstance(_raw_start, (int, float)):
                    state.start_time = datetime.fromtimestamp(
                        _raw_start, tz=timezone.utc
                    )
                elif isinstance(_raw_start, datetime):
                    state.start_time = _raw_start
                else:
                    state.start_time = datetime.now(timezone.utc)
                logger.info(
                    f"[TripManager] {device_id} "
                    f"trip start at {state.start_time}"
                )

        # ── CASE 2: ignition OFF ──────────────────────────────
        else:

            if state.is_running and device_id not in TRIP_END_TASKS:

                # payload["ts"] อาจเป็น int/float → แปลงเป็น datetime
                _raw_end = payload.get("ts", None)
                if isinstance(_raw_end, (int, float)):
                    end_time = datetime.fromtimestamp(
                        _raw_end, tz=timezone.utc
                    )
                elif isinstance(_raw_end, datetime):
                    end_time = _raw_end
                else:
                    end_time = datetime.now(timezone.utc)

                state.last_ignition_off_time = end_time

                logger.info(
                    f"[TripManager] {device_id} "
                    f"ignition OFF — debounce {DEBOUNCE_SECONDS}s start"
                )

                task = asyncio.create_task(
                    _debounce_and_finalize(
                        pool,
                        device_id,
                        state.start_time,
                        end_time,
                    ),
                    name=f"debounce-{device_id}",
                )

                TRIP_END_TASKS[device_id] = task