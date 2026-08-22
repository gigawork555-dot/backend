from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Security
import asyncpg

from app.config import settings
from app.auth.dependencies import verify_api_key

router = APIRouter(prefix="/api/v1/reports", tags=["Reports"])


async def _get_db() -> asyncpg.Connection:
    return await asyncpg.connect(
        user=settings.DB_USER,
        password=settings.DB_PASS,
        database=settings.DB_NAME,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
    )


async def _get_tier_thresholds(conn: asyncpg.Connection) -> tuple[float, float, float]:
    return 90.0, 75.0, 60.0


@router.get("/driver-score")
async def report_driver_score(
    months:     int           = Query(default=3,  ge=1, le=24,
                                      description="ย้อนหลัง N เดือน"),
    page:       int           = Query(default=1,  ge=1),
    limit:      int           = Query(default=50, ge=1, le=200),
    driver_id:  Optional[int] = Query(default=None,
                                      description="กรองพนักงานเดี่ยว"),
    tier_a_min: float         = Query(default=90.0, description="Tier A min score"),
    tier_b_min: float         = Query(default=75.0, description="Tier B min score"),
    tier_c_min: float         = Query(default=60.0, description="Tier C min score"),
    api_key: dict = Security(verify_api_key),
):
    offset = (page - 1) * limit

    try:
        conn = await _get_db()

        where_parts = [
            "trip_start >= NOW() - ($1 || ' months')::interval",
            "trip_end IS NOT NULL",
        ]
        params: list = [str(months)]

        if driver_id is not None:
            params.append(driver_id)
            where_parts.append(f"driver_id = ${len(params)}")

        where_sql = " AND ".join(where_parts)

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
                "safe_trip_threshold": tier_b_min,
            },
            "data": result_data,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fleet-summary")
async def report_fleet_summary(
    days:    int = Query(default=7, ge=1, le=365,
                         description="ย้อนหลัง N วัน"),
    api_key: dict = Security(verify_api_key),
):
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


@router.get("/fuel-efficiency")
async def report_fuel_efficiency(
    days:    int = Query(default=30, ge=1, le=365,
                         description="ย้อนหลัง N วัน"),
    api_key: dict = Security(verify_api_key),
):
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


@router.get("/maintenance-forecast")
async def report_maintenance_forecast(
    lookback_days:       int   = Query(default=30, ge=1, le=365,
                                       description="ช่วงเวลาย้อนหลัง (วัน)"),
    km_high:             int   = Query(default=5000,
                                       description="ระยะทางขั้นสูง → priority สูง"),
    km_medium:           int   = Query(default=2000,
                                       description="ระยะทางขั้นกลาง → priority กลาง"),
    engine_hours_high:   float = Query(default=200.0,
                                       description="ชม.เดินเครื่องขั้นสูง → priority สูง"),
    engine_hours_medium: float = Query(default=100.0,
                                       description="ชม.เดินเครื่องขั้นกลาง → priority กลาง"),
    days_since_service:  int   = Query(default=90,
                                       description="วันตั้งแต่ทริปล่าสุด → แจ้งเตือน"),
    harsh_brake_limit:   int   = Query(default=20,
                                       description="จำนวนเบรคหักสะสมที่ต้องแจ้ง"),
    api_key: dict = Security(verify_api_key),
):
    try:
        conn = await _get_db()

        rows = await conn.fetch(
            """
            SELECT
                vehicle_id,
                COUNT(*)                                              AS total_trips,
                ROUND(SUM(distance_km)::numeric, 2)                  AS total_distance_km,
                ROUND(SUM(duration_min)::numeric, 2)                 AS total_duration_min,
                ROUND((SUM(duration_min) / 60.0)::numeric, 2)        AS total_engine_hours,
                SUM(harsh_brake_count)                               AS total_harsh_brake,
                SUM(harsh_accel_count)                               AS total_harsh_accel,
                SUM(harsh_corner_count)                              AS total_harsh_corner,
                ROUND(AVG(driver_score)::numeric, 2)                 AS avg_score,
                MAX(trip_end)                                        AS last_trip,
                EXTRACT(DAY FROM NOW() - MAX(trip_end))::int         AS days_since_last_trip,
                CASE
                    WHEN SUM(distance_km) >= $2 THEN 'สูง'
                    WHEN SUM(distance_km) >= $3 THEN 'กลาง'
                    ELSE 'ต่ำ'
                END                                                  AS distance_priority,
                CASE
                    WHEN (SUM(duration_min) / 60.0) >= $4 THEN 'สูง'
                    WHEN (SUM(duration_min) / 60.0) >= $5 THEN 'กลาง'
                    ELSE 'ต่ำ'
                END                                                  AS engine_hours_priority,
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
            str(lookback_days),
            km_high,
            km_medium,
            engine_hours_high,
            engine_hours_medium,
            days_since_service,
            harsh_brake_limit,
        )

        await conn.close()

        data = [dict(r) for r in rows]

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
