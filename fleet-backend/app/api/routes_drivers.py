from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Security
import asyncpg

from app.config import settings
from app.auth.dependencies import verify_api_key

router = APIRouter(
    prefix="/api/v1/drivers",
    tags=["Drivers & Incentive Rewards"],
)


async def _get_db() -> asyncpg.Connection:
    return await asyncpg.connect(
        user=settings.DB_USER,
        password=settings.DB_PASS,
        database=settings.DB_NAME,
        host=settings.DB_HOST,
        port=settings.DB_PORT,
    )


def _parse_driver_id(driver_id: str) -> int:
    return int(driver_id) if driver_id.isdigit() else 0


async def _get_active_config(conn: asyncpg.Connection) -> dict:
    row = await conn.fetchrow(
        """
        SELECT
            score_base,
            harsh_brake_deduct,
            harsh_accel_deduct,
            harsh_corner_deduct,
            speeding_deduct,
            idling_deduct,
            bump_deduct,
            max_deduct_per_trip
        FROM scoring_config_cache
        WHERE is_active = TRUE
        ORDER BY effective_date DESC
        LIMIT 1
        """
    )
    if row:
        return dict(row)

    return {
        "score_base":           100.0,
        "harsh_brake_deduct":     5.0,
        "harsh_accel_deduct":     3.0,
        "harsh_corner_deduct":    3.0,
        "speeding_deduct":       10.0,
        "idling_deduct":          2.0,
        "bump_deduct":            4.0,
        "max_deduct_per_trip":   50.0,
    }


def _calc_tier(
    avg_score: float,
    tier_a_min: float,
    tier_b_min: float,
    tier_c_min: float,
) -> tuple[str, float]:
    if avg_score >= tier_a_min:
        return "A", 10.0
    if avg_score >= tier_b_min:
        return "B", 5.0
    if avg_score >= tier_c_min:
        return "C", 0.0
    return "D", 0.0


@router.get("/{driver_id}/bonus")
async def get_driver_bonus_summary(
    driver_id: str,
    month: int = Query(
        default=0,
        description="เดือน 1-12 (0 = เดือนปัจจุบัน)"
    ),
    year: int = Query(
        default=0,
        description="ปี เช่น 2568 (0 = ปีปัจจุบัน)"
    ),
    tier_a_min: float = Query(
        default=90.0,
        description="FDD §12.3 Tier A ขั้นต่ำ (default 90)"
    ),
    tier_b_min: float = Query(
        default=75.0,
        description="FDD §12.3 Tier B ขั้นต่ำ (default 75)"
    ),
    tier_c_min: float = Query(
        default=60.0,
        description="FDD §12.3 Tier C ขั้นต่ำ (default 60)"
    ),
    api_key: dict = Security(verify_api_key),
):
    did = _parse_driver_id(driver_id)

    now = datetime.now(timezone.utc)
    target_month = month if 1 <= month <= 12 else now.month
    target_year  = year  if year  > 2000     else now.year

    try:
        conn = await get_db_connection()

        rows = await conn.fetch(
            """
            SELECT
                id,
                driver_score,
                distance_km,
                harsh_brake_count,
                harsh_accel_count,
                harsh_corner_count,
                speeding_count,
                idle_min
            FROM trip_logs
            WHERE driver_id = $1
              AND EXTRACT(MONTH FROM trip_start) = $2
              AND EXTRACT(YEAR  FROM trip_start) = $3
              AND trip_end IS NOT NULL
            ORDER BY trip_start ASC
            """,
            did,
            target_month,
            target_year,
        )

        config = await _get_active_config(conn)
        await conn.close()

        if not rows:
            tier, bonus_pct = _calc_tier(0.0, tier_a_min, tier_b_min, tier_c_min)
            return {
                "driver_id":       driver_id,
                "period_month":    target_month,
                "period_year":     target_year,
                "total_trips":     0,
                "avg_score":       None,
                "min_score":       None,
                "incentive_tier":  tier,
                "bonus_pct":       bonus_pct,
                "tier_thresholds": {
                    "tier_a_min": tier_a_min,
                    "tier_b_min": tier_b_min,
                    "tier_c_min": tier_c_min,
                },
                "total_harsh_events":     0,
                "total_idle_min":         0.0,
                "total_distance_km":      0.0,
                "note": (
                    "bonus_amount = bonus_pct × hr.contract.wage "
                    "คำนวณโดย Odoo (FDD §12.4)"
                ),
                "scoring_config_snapshot": config,
            }

        scores = [float(r["driver_score"]) for r in rows]
        avg_score = round(sum(scores) / len(scores), 2)
        min_score = round(min(scores), 2)

        total_harsh = sum(
            (r["harsh_brake_count"] or 0)
            + (r["harsh_accel_count"] or 0)
            + (r["harsh_corner_count"] or 0)
            + (r["speeding_count"] or 0)
            for r in rows
        )
        total_idle_min   = round(sum(float(r["idle_min"] or 0) for r in rows), 2)
        total_distance   = round(sum(float(r["distance_km"] or 0) for r in rows), 2)

        tier, bonus_pct = _calc_tier(avg_score, tier_a_min, tier_b_min, tier_c_min)

        return {
            "driver_id":          driver_id,
            "period_month":       target_month,
            "period_year":        target_year,
            "total_trips":        len(rows),
            "avg_score":          avg_score,
            "min_score":          min_score,
            "incentive_tier":     tier,
            "bonus_pct":          bonus_pct,
            "tier_thresholds": {
                "tier_a_min": tier_a_min,
                "tier_b_min": tier_b_min,
                "tier_c_min": tier_c_min,
            },
            "total_harsh_events": total_harsh,
            "total_idle_min":     total_idle_min,
            "total_distance_km":  total_distance,
            "note": (
                "bonus_amount = bonus_pct × hr.contract.wage "
                "คำนวณโดย Odoo (FDD §12.4)"
            ),
            "scoring_config_snapshot": config,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def get_db_connection() -> asyncpg.Connection:
    return await _get_db()


@router.get("/{driver_id}/score")
async def get_driver_score(
    driver_id: str,
    tier_a_min: float = Query(default=90.0),
    tier_b_min: float = Query(default=75.0),
    tier_c_min: float = Query(default=60.0),
    api_key: dict = Security(verify_api_key),
):
    did = _parse_driver_id(driver_id)

    try:
        conn = await get_db_connection()

        summary = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                    AS total_trips,
                ROUND(AVG(driver_score)::numeric, 2)        AS avg_score,
                MAX(driver_score)                           AS max_score,
                MIN(driver_score)                           AS min_score,
                ROUND(SUM(distance_km)::numeric, 2)         AS total_distance_km,
                ROUND(SUM(idle_min)::numeric, 2)            AS total_idle_min,
                SUM(harsh_brake_count)                      AS total_harsh_brake,
                SUM(harsh_accel_count)                      AS total_harsh_accel,
                SUM(harsh_corner_count)                     AS total_harsh_corner,
                SUM(speeding_count)                         AS total_speeding
            FROM trip_logs
            WHERE driver_id = $1
              AND trip_end IS NOT NULL
            """,
            did,
        )

        trend = await conn.fetch(
            """
            SELECT
                TO_CHAR(DATE_TRUNC('month', trip_start), 'YYYY-MM') AS month,
                COUNT(*)                                             AS trips,
                ROUND(AVG(driver_score)::numeric, 2)                AS avg_score,
                ROUND(MIN(driver_score)::numeric, 2)                AS min_score,
                ROUND(SUM(distance_km)::numeric, 2)                 AS total_km,
                SUM(harsh_brake_count
                    + harsh_accel_count
                    + harsh_corner_count
                    + speeding_count)                                AS total_harsh_events,
                ROUND(SUM(idle_min)::numeric, 2)                    AS total_idle_min
            FROM trip_logs
            WHERE driver_id = $1
              AND trip_start >= NOW() - INTERVAL '6 months'
              AND trip_end IS NOT NULL
            GROUP BY DATE_TRUNC('month', trip_start)
            ORDER BY month DESC
            """,
            did,
        )

        await conn.close()
        avg = float((summary["avg_score"] if summary else 0) or 0)
        tier, bonus_pct = _calc_tier(avg, tier_a_min, tier_b_min, tier_c_min)

        hr_alert = tier == "D"

        return {
            "driver_id":      driver_id,
            "summary":        dict(summary) if summary else {},
            "incentive_tier": tier,
            "bonus_pct":      bonus_pct,
            "hr_alert":       hr_alert,
            "tier_thresholds": {
                "tier_a_min": tier_a_min,
                "tier_b_min": tier_b_min,
                "tier_c_min": tier_c_min,
            },
            "monthly_trend": [dict(t) for t in trend],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{driver_id}/events")
async def get_driver_events(
    driver_id:  str,
    page:       int           = Query(default=1, ge=1),
    limit:      int           = Query(default=50, ge=1, le=500),
    event_type: Optional[str] = Query(
        default=None,
        description="กรองตาม event type: harsh_brake|harsh_acceleration|harsh_cornering|speeding|idling"
    ),
    date_from:  Optional[datetime] = Query(default=None),
    date_to:    Optional[datetime] = Query(default=None),
    api_key: dict = Security(verify_api_key),
):
    did = _parse_driver_id(driver_id)
    offset = (page - 1) * limit

    try:
        conn = await get_db_connection()

        trips = await conn.fetch(
            "SELECT DISTINCT device_id FROM trip_logs WHERE driver_id = $1",
            did,
        )
        device_ids = [t["device_id"] for t in trips]

        if not device_ids:
            await conn.close()
            return {"driver_id": driver_id, "events": [], "total": 0, "page": page}

        where_parts = [
            "device_id = ANY($1::text[])",
            "event IS NOT NULL",
            "event != ''",
        ]
        params: list = [device_ids]

        if event_type:
            params.append(event_type)
            where_parts.append(f"event = ${len(params)}")

        if date_from:
            params.append(date_from)
            where_parts.append(f"ts >= ${len(params)}")

        if date_to:
            params.append(date_to)
            where_parts.append(f"ts <= ${len(params)}")

        where_sql = " AND ".join(where_parts)

        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM telemetry_raw WHERE {where_sql}",
            *params,
        )

        params_paged = params + [limit, offset]
        events = await conn.fetch(
            f"""
            SELECT
                ts, device_id, lat, lon,
                speed, event, event_severity,
                ax, ay, az
            FROM telemetry_raw
            WHERE {where_sql}
            ORDER BY ts DESC
            LIMIT ${len(params_paged) - 1} OFFSET ${len(params_paged)}
            """,
            *params_paged,
        )

        await conn.close()

        return {
            "driver_id":   driver_id,
            "total":       total,
            "page":        page,
            "limit":       limit,
            "total_pages": max(1, -(-total // limit)),
            "filters": {
                "event_type": event_type,
                "date_from":  date_from.isoformat() if date_from else None,
                "date_to":    date_to.isoformat()   if date_to   else None,
            },
            "events": [dict(e) for e in events],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{driver_id}/fuel-summary")
async def get_driver_fuel_summary(
    driver_id: str,
    months: int = Query(default=1, ge=1, le=12,
                        description="ย้อนหลัง N เดือน (default 1 = เดือนปัจจุบัน)"),
    api_key: dict = Security(verify_api_key),
):
    did = _parse_driver_id(driver_id)

    try:
        conn = await get_db_connection()

        summary = await conn.fetchrow(
            """
            SELECT
                COUNT(*)                                         AS total_trips,
                ROUND(SUM(fuel_used)::numeric,    2)             AS total_fuel_used,
                ROUND(AVG(fuel_used)::numeric,    2)             AS avg_fuel_per_trip,
                ROUND(SUM(distance_km)::numeric,  2)             AS total_distance_km,
                ROUND(SUM(idle_min)::numeric,     2)             AS total_idle_min,
                ROUND(
                    CASE
                        WHEN SUM(distance_km) > 0
                        THEN SUM(fuel_used) / SUM(distance_km) * 100
                        ELSE 0
                    END::numeric, 2
                )                                                AS avg_fuel_per_100km,
                ROUND(
                    (SUM(idle_min) / 60.0 * 0.8)::numeric, 2
                )                                                AS estimated_idle_fuel_cost_liters
            FROM trip_logs
            WHERE driver_id = $1
              AND trip_start >= NOW() - ($2 || ' months')::interval
              AND trip_end IS NOT NULL
            """,
            did,
            str(months),
        )

        await conn.close()

        result = dict(summary) if summary else {}
        result["driver_id"] = driver_id
        result["unit"]       = "ลิตร"
        result["period_months"] = months

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
