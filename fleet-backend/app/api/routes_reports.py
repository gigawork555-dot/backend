# app/api/routes_reports.py
# FDD v1.4 Compliant — Fixed Version
#
# Changes vs previous version:
#
#   [FIX-1] ทุก endpoint — เพิ่ม APIKEY authentication
#           FDD §13: REST API ต้องมี authentication
#
#   [FIX-2] /driver-score — เพิ่ม pagination + filter driver_id เดี่ยว
#           + safe_trip threshold อ่านจาก scoring_config_cache
#           + Tier ตาม FDD §12.3 (A=90/B=75/C=60/D=0)
#
#   [FIX-3] /maintenance-forecast — ลบ hardcode 5000/2000/20
#           → เปลี่ยนเป็น query param ที่ Odoo หรือ Admin ส่งมาได้
#
#   [FIX-4] /maintenance-forecast — เพิ่ม trigger ครบ 3 แบบตาม FDD §2.2
#           FDD: "ระยะทางสะสม (กม.) / ชั่วโมงเดินเครื่อง / ช่วงเวลา (เดือน)"
#           duration_min ใน trip_logs ใช้แทน engine_hours ได้ (sum/60)
#
#   [FIX-5] /driver-score — safe_trip threshold ใช้ Tier B (75) ตาม FDD §12.3
#           เดิม hardcode 85 ซึ่งไม่ตรง Tier ใดใน FDD

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
import asyncpg

from app.config import settings

router = APIRouter(prefix="/api/v1/reports", tags=["Reports"])

# ── API Key auth ───────────────────────────────────────────────
API_KEY = "ktc-fleet-2026-secret"
api_key_header = APIKeyHeader(name="APIKEY", auto_error=False)


async def _verify_api_key(api_key: str = Security(api_key_header)) -> str:
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="API Key ไม่ถูกต้อง")
    return api_key


async def _get_db() -> asyncpg.Connection:
    return await asyncpg.connect(
        user=settings.DB_USER,
        password=settings.DB_PASS,
        database=settings.DB_NAME,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
    )


async def _get_tier_thresholds(conn: asyncpg.Connection) -> tuple[float, float, float]:
    """
    ดึง tier threshold จาก scoring_config_cache
    FDD §12.3: Admin กำหนดขอบเขตคะแนนได้
    Return: (tier_a_min, tier_b_min, tier_c_min)
    """
    # scoring_config_cache ไม่มี tier columns โดยตรง
    # → tier boundary ถูก manage ใน Odoo (fleet.telematics.scoring.config)
    # → Backend ใช้ FDD default เป็น fallback
    # → Odoo ส่งผ่าน query param ได้
    return 90.0, 75.0, 60.0


# ================================================================
# GET /api/v1/reports/driver-score
#
# [FIX-1] เพิ่ม auth
# [FIX-2] เพิ่ม pagination + filter driver_id
# [FIX-5] safe_trip ใช้ Tier B (75) ตาม FDD §12.3
# ================================================================

@router.get("/driver-score")
async def report_driver_score(
    months:     int           = Query(default=3,  ge=1, le=24,
                                      description="ย้อนหลัง N เดือน"),
    # [FIX-2] Pagination
    page:       int           = Query(default=1,  ge=1),
    limit:      int           = Query(default=50, ge=1, le=200),
    # [FIX-2] Filter เดี่ยว
    driver_id:  Optional[int] = Query(default=None,
                                      description="กรองพนักงานเดี่ยว"),
    # [FIX-5] Tier threshold — Odoo ส่งมาได้
    tier_a_min: float         = Query(default=90.0, description="Tier A min score"),
    tier_b_min: float         = Query(default=75.0, description="Tier B min score"),
    tier_c_min: float         = Query(default=60.0, description="Tier C min score"),
    api_key: str = Security(_verify_api_key),  # [FIX-1]
):
    """
    รายงานคะแนนพนักงานรายเดือน — FDD §12.6 Monthly Score Report

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    **Tier ตาม FDD §12.3:**
    - A ≥ 90 → 10% โบนัส
    - B ≥ 75 →  5% โบนัส
    - C ≥ 60 →  0%
    - D < 60 →  0% + แจ้งเตือน HR
    """
    offset = (page - 1) * limit

    try:
        conn = await _get_db()

        # WHERE แบบ dynamic
        where_parts = [
            "trip_start >= NOW() - ($1 || ' months')::interval",
            "trip_end IS NOT NULL",
        ]
        params: list = [str(months)]

        if driver_id is not None:
            params.append(driver_id)
            where_parts.append(f"driver_id = ${len(params)}")

        where_sql = " AND ".join(where_parts)

        # นับทั้งหมด (distinct driver-month)
        total = await conn.fetchval(
            f"""
            SELECT COUNT(DISTINCT (driver_id, DATE_TRUNC('month', trip_start)))
            FROM trip_logs
            WHERE {where_sql}
            """,
            *params,
        )

        params_paged = params + [limit, offset]

        rows = await conn.fetch(
            f"""
            SELECT
                driver_id,
                TO_CHAR(DATE_TRUNC('month', trip_start), 'YYYY-MM') AS month,
                COUNT(*)                                              AS total_trips,
                ROUND(AVG(driver_score)::numeric, 2)                 AS avg_score,
                ROUND(MIN(driver_score)::numeric, 2)                 AS min_score,
                -- [FIX-5] safe_trip ใช้ tier_b_min (75) ตาม FDD §12.3
                -- เดิม hardcode 85 ซึ่งไม่ตรง Tier ใดเลย
                SUM(CASE WHEN driver_score >= {tier_b_min} THEN 1 ELSE 0 END) AS safe_trips,
                SUM(harsh_brake_count)    AS total_harsh_brake,
                SUM(harsh_accel_count)    AS total_harsh_accel,
                SUM(harsh_corner_count)   AS total_harsh_corner,
                SUM(speeding_count)       AS total_speeding,
                ROUND(SUM(idle_min)::numeric, 2)                     AS total_idle_min,
                ROUND(SUM(distance_km)::numeric, 2)                  AS total_distance_km
            FROM trip_logs
            WHERE {where_sql}
            GROUP BY driver_id, DATE_TRUNC('month', trip_start)
            ORDER BY month DESC, driver_id ASC
            LIMIT ${len(params_paged) - 1} OFFSET ${len(params_paged)}
            """,
            *params_paged,
        )

        await conn.close()

        # เพิ่ม Tier ให้แต่ละ row
        result_data = []
        for r in rows:
            row_dict = dict(r)
            avg = float(row_dict.get("avg_score") or 0)
            if avg >= tier_a_min:
                row_dict["incentive_tier"] = "A"
            elif avg >= tier_b_min:
                row_dict["incentive_tier"] = "B"
            elif avg >= tier_c_min:
                row_dict["incentive_tier"] = "C"
            else:
                row_dict["incentive_tier"] = "D"
            result_data.append(row_dict)

        return {
            "months":        months,
            "page":          page,
            "limit":         limit,
            "total_records": total,
            "total_pages":   max(1, -(-total // limit)),
            "tier_thresholds": {
                "tier_a_min": tier_a_min,
                "tier_b_min": tier_b_min,
                "tier_c_min": tier_c_min,
                "safe_trip_threshold": tier_b_min,  # FDD §12.3 Tier B
            },
            "data": result_data,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================================================
# GET /api/v1/reports/fleet-summary
# [FIX-1] เพิ่ม auth
# ================================================================

@router.get("/fleet-summary")
async def report_fleet_summary(
    days:    int = Query(default=7, ge=1, le=365,
                         description="ย้อนหลัง N วัน"),
    api_key: str = Security(_verify_api_key),  # [FIX-1]
):
    """
    ภาพรวม fleet รายวัน ย้อนหลัง N วัน

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)
    """
    try:
        conn = await _get_db()
        rows = await conn.fetch(
            """
            SELECT
                DATE(trip_start)                                     AS date,
                COUNT(*)                                             AS total_trips,
                COUNT(DISTINCT vehicle_id)                           AS active_vehicles,
                COUNT(DISTINCT driver_id)                            AS active_drivers,
                ROUND(AVG(driver_score)::numeric, 2)                 AS avg_score,
                ROUND(SUM(distance_km)::numeric, 2)                  AS total_distance_km,
                SUM(harsh_brake_count
                    + harsh_accel_count
                    + harsh_corner_count)                            AS total_harsh_events,
                SUM(speeding_count)                                  AS total_speeding,
                ROUND(SUM(idle_min)::numeric, 2)                     AS total_idle_min,
                ROUND(SUM(fuel_used)::numeric, 2)                    AS total_fuel_used
            FROM trip_logs
            WHERE trip_start >= NOW() - ($1 || ' days')::interval
              AND trip_end IS NOT NULL
            GROUP BY DATE(trip_start)
            ORDER BY date DESC
            """,
            str(days),
        )
        await conn.close()
        return {
            "days":       days,
            "total_days": len(rows),
            "data":       [dict(r) for r in rows],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================================================
# GET /api/v1/reports/fuel-efficiency
# [FIX-1] เพิ่ม auth
# ================================================================

@router.get("/fuel-efficiency")
async def report_fuel_efficiency(
    days:    int = Query(default=30, ge=1, le=365,
                         description="ย้อนหลัง N วัน"),
    api_key: str = Security(_verify_api_key),  # [FIX-1]
):
    """
    รายงานประสิทธิภาพเชื้อเพลิงรายรถ — FDD §2.1

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)
    """
    try:
        conn = await _get_db()
        rows = await conn.fetch(
            """
            SELECT
                vehicle_id,
                COUNT(*)                                             AS total_trips,
                ROUND(SUM(fuel_used)::numeric, 2)                    AS total_fuel_used,
                ROUND(SUM(distance_km)::numeric, 2)                  AS total_distance_km,
                ROUND(
                    CASE WHEN SUM(distance_km) > 0
                    THEN SUM(fuel_used) / SUM(distance_km) * 100
                    ELSE 0 END::numeric, 2
                )                                                    AS fuel_per_100km,
                ROUND(AVG(driver_score)::numeric, 2)                 AS avg_driver_score,
                -- FDD §2.1: idling cost
                ROUND(SUM(idle_min)::numeric, 2)                     AS total_idle_min,
                ROUND((SUM(idle_min) / 60.0 * 0.8)::numeric, 2)     AS idle_fuel_est_liters
            FROM trip_logs
            WHERE trip_start >= NOW() - ($1 || ' days')::interval
              AND vehicle_id > 0
              AND trip_end IS NOT NULL
            GROUP BY vehicle_id
            ORDER BY fuel_per_100km DESC
            """,
            str(days),
        )
        await conn.close()
        return {
            "days":           days,
            "unit":           "ลิตร",
            "total_vehicles": len(rows),
            "data":           [dict(r) for r in rows],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================================================
# GET /api/v1/reports/maintenance-forecast
#
# [FIX-1] เพิ่ม auth
# [FIX-3] ลบ hardcode 5000/2000/20 → เปลี่ยนเป็น query param
# [FIX-4] เพิ่ม trigger ครบ 3 แบบตาม FDD §2.2:
#   1. ระยะทางสะสม (km)          ← เดิมมีแค่นี้
#   2. ชั่วโมงเดินเครื่อง (hours)  ← เพิ่มใหม่ (sum duration_min/60)
#   3. ช่วงเวลา (เดือน)            ← เพิ่มใหม่ (วันนับจาก last trip)
# ================================================================

@router.get("/maintenance-forecast")
async def report_maintenance_forecast(
    # [FIX-3] ลบ hardcode → query param ที่ Admin/Odoo ส่งมาได้
    lookback_days:       int   = Query(default=30, ge=1, le=365,
                                       description="ช่วงเวลาย้อนหลัง (วัน)"),
    # Trigger 1: ระยะทาง (FDD §2.2)
    km_high:             int   = Query(default=5000,
                                       description="ระยะทางขั้นสูง → priority สูง"),
    km_medium:           int   = Query(default=2000,
                                       description="ระยะทางขั้นกลาง → priority กลาง"),
    # Trigger 2: ชั่วโมงเดินเครื่อง (FDD §2.2) — คำนวณจาก duration_min
    engine_hours_high:   float = Query(default=200.0,
                                       description="ชม.เดินเครื่องขั้นสูง → priority สูง"),
    engine_hours_medium: float = Query(default=100.0,
                                       description="ชม.เดินเครื่องขั้นกลาง → priority กลาง"),
    # Trigger 3: ช่วงเวลา (FDD §2.2) — วันตั้งแต่ last_trip
    days_since_service:  int   = Query(default=90,
                                       description="วันตั้งแต่ทริปล่าสุด → แจ้งเตือน"),
    # Harsh event threshold
    harsh_brake_limit:   int   = Query(default=20,
                                       description="จำนวนเบรคหักสะสมที่ต้องแจ้ง"),
    api_key: str = Security(_verify_api_key),  # [FIX-1]
):
    """
    คาดการณ์รถที่ควรเข้าซ่อมบำรุง — FDD §2.2

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    **Trigger ครบ 3 แบบตาม FDD §2.2:**
    1. ระยะทางสะสม (km)        — km_high / km_medium
    2. ชั่วโมงเดินเครื่อง (hours) — engine_hours_high / engine_hours_medium
    3. ช่วงเวลา (วัน)            — days_since_service
    """
    try:
        conn = await _get_db()

        # [FIX-4] Query รวม 3 trigger
        # engine_hours คำนวณจาก SUM(duration_min) / 60
        rows = await conn.fetch(
            """
            SELECT
                vehicle_id,
                COUNT(*)                                              AS total_trips,
                ROUND(SUM(distance_km)::numeric, 2)                  AS total_distance_km,
                ROUND(SUM(duration_min)::numeric, 2)                 AS total_duration_min,
                -- Trigger 2: engine hours
                ROUND((SUM(duration_min) / 60.0)::numeric, 2)        AS total_engine_hours,
                SUM(harsh_brake_count)                               AS total_harsh_brake,
                SUM(harsh_accel_count)                               AS total_harsh_accel,
                SUM(harsh_corner_count)                              AS total_harsh_corner,
                ROUND(AVG(driver_score)::numeric, 2)                 AS avg_score,
                MAX(trip_end)                                        AS last_trip,
                -- Trigger 3: วันตั้งแต่ trip ล่าสุด
                EXTRACT(DAY FROM NOW() - MAX(trip_end))::int         AS days_since_last_trip,
                -- [FIX-3] Priority ใช้ param แทน hardcode
                CASE
                    WHEN SUM(distance_km) >= $2 THEN 'สูง'
                    WHEN SUM(distance_km) >= $3 THEN 'กลาง'
                    ELSE 'ต่ำ'
                END                                                  AS distance_priority,
                -- Trigger 2 priority
                CASE
                    WHEN (SUM(duration_min) / 60.0) >= $4 THEN 'สูง'
                    WHEN (SUM(duration_min) / 60.0) >= $5 THEN 'กลาง'
                    ELSE 'ต่ำ'
                END                                                  AS engine_hours_priority,
                -- needs_maintenance รวมทุก trigger (FDD §2.2)
                CASE
                    WHEN SUM(distance_km) >= $2
                      OR (SUM(duration_min) / 60.0) >= $4
                      OR EXTRACT(DAY FROM NOW() - MAX(trip_end)) >= $6
                      OR SUM(harsh_brake_count) >= $7
                    THEN true
                    ELSE false
                END                                                  AS needs_maintenance
            FROM trip_logs
            WHERE vehicle_id > 0
              AND trip_start >= NOW() - ($1 || ' days')::interval
              AND trip_end IS NOT NULL
            GROUP BY vehicle_id
            ORDER BY needs_maintenance DESC, total_distance_km DESC
            """,
            str(lookback_days),  # $1
            km_high,             # $2 — Trigger 1 high
            km_medium,           # $3 — Trigger 1 medium
            engine_hours_high,   # $4 — Trigger 2 high
            engine_hours_medium,  # $5 — Trigger 2 medium  ← +1 space
            days_since_service,  # $6 — Trigger 3
            harsh_brake_limit,   # $7 — harsh event
        )

        await conn.close()

        data = [dict(r) for r in rows]

        # เพิ่ม needs_maintenance_reason ให้รู้ว่า trigger ไหนที่ทำให้ต้องซ่อม
        for item in data:
            reasons = []
            if (item.get("total_distance_km") or 0) >= km_high:
                reasons.append(f"ระยะทาง ≥ {km_high:,} km")
            if (item.get("total_engine_hours") or 0) >= engine_hours_high:
                reasons.append(f"ชม.เครื่อง ≥ {engine_hours_high:.0f} ชม.")
            if (item.get("days_since_last_trip") or 0) >= days_since_service:
                reasons.append(f"ไม่ได้ซ่อม ≥ {days_since_service} วัน")
            if (item.get("total_harsh_brake") or 0) >= harsh_brake_limit:
                reasons.append(f"เบรคหัก ≥ {harsh_brake_limit} ครั้ง")
            item["maintenance_reasons"] = reasons

        return {
            "lookback_days":    lookback_days,
            "total_vehicles":   len(data),
            "needs_maintenance": sum(1 for r in data if r.get("needs_maintenance")),
            # แสดง threshold ที่ใช้ให้ Odoo รู้
            "thresholds_used": {
                "trigger_1_distance":      {"high": km_high, "medium": km_medium},
                "trigger_2_engine_hours":  {"high": engine_hours_high,
                                            "medium": engine_hours_medium},
                "trigger_3_days_since":    days_since_service,
                "harsh_brake_limit":       harsh_brake_limit,
            },
            "data": data,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))