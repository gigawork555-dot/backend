from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Security
from pydantic import BaseModel
import asyncpg

from app.database import get_db_pool
from app.auth.dependencies import verify_api_key

router = APIRouter(prefix="/api/v1", tags=["Trips"])


class MarkSyncedRequest(BaseModel):
    synced_at: Optional[datetime] = None


class MarkSyncedResponse(BaseModel):
    status: str
    trip_id: int
    synced_to_odoo: bool
    synced_at: Optional[datetime]


class BatchMarkSyncedRequest(BaseModel):
    trip_ids: list[int]


class OdooSyncWebhookRequest(BaseModel):
    last_sync_timestamp: Optional[datetime] = None


@router.post(
    "/webhook/odoo-sync",
    summary="[FDD §11.3] Odoo pull trip logs ที่ยังไม่ sync (batch ≤ 200 records)",
    tags=["Trips"],
)
async def odoo_sync_webhook(
    request: OdooSyncWebhookRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    try:
        where_clauses = ["synced_to_odoo = false"]
        params: list = []

        if request.last_sync_timestamp is not None:
            params.append(request.last_sync_timestamp)
            where_clauses.append(f"created_at > ${len(params)}")

        where_sql = " AND ".join(where_clauses)

        query_ts = datetime.utcnow()

        trips = await pool.fetch(
            f"""
            SELECT
                id, device_id, vehicle_id, driver_id,
                trip_start, trip_end,
                distance_km, duration_min, idle_min,
                max_speed, avg_speed,
                harsh_brake_count, harsh_accel_count,
                harsh_corner_count, speeding_count,
                driver_score, fuel_used,
                gps_track,
                synced_to_odoo, synced_at,
                created_at
            FROM trip_logs
            WHERE {where_sql}
            ORDER BY created_at ASC
            LIMIT 200
            """,
            *params,
        )

        trip_list = [dict(t) for t in trips]

        return {
            "total":               len(trip_list),
            "last_sync_timestamp": query_ts,
            "trips":               trip_list,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trips/unsynced")
async def get_unsynced_trips(
    vehicle_id: Optional[int]      = None,
    device_id:  Optional[str]      = None,
    driver_id:  Optional[int]      = None,
    since:      Optional[datetime] = None,
    last_id:    Optional[int]      = None,
    limit:      int                = 100,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    try:
        where_clauses = ["synced_to_odoo = false"]
        params = []

        if vehicle_id is not None:
            params.append(vehicle_id)
            where_clauses.append(f"vehicle_id = ${len(params)}")

        if device_id is not None:
            params.append(device_id)
            where_clauses.append(f"device_id = ${len(params)}")

        if driver_id is not None:
            params.append(driver_id)
            where_clauses.append(f"driver_id = ${len(params)}")

        if since is not None:
            params.append(since)
            where_clauses.append(f"created_at >= ${len(params)}")

        if last_id is not None:
            params.append(last_id)
            where_clauses.append(f"id > ${len(params)}")

        where_sql = " AND ".join(where_clauses)

        trips = await pool.fetch(
            f"""
            SELECT
                id, device_id, vehicle_id, driver_id,
                trip_start, trip_end,
                distance_km, duration_min, idle_min,
                max_speed, avg_speed,
                harsh_brake_count, harsh_accel_count,
                harsh_corner_count, speeding_count,
                driver_score, fuel_used,
                created_at
            FROM trip_logs
            WHERE {where_sql}
            ORDER BY trip_start ASC
            LIMIT {limit}
            """,
            *params,
        )

        trip_list = [dict(t) for t in trips]
        return {
            "total":   len(trip_list),
            "last_id": trip_list[-1]["id"] if trip_list else None,
            "trips":   trip_list,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/trips/batch/mark-synced", status_code=200)
async def mark_trips_synced_batch(
    request: BatchMarkSyncedRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    if not request.trip_ids:
        raise HTTPException(status_code=400, detail="trip_ids ว่างเปล่า")

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    UPDATE trip_logs
                    SET synced_to_odoo = true,
                        synced_at      = NOW()
                    WHERE id = ANY($1::bigint[])
                      AND synced_to_odoo = false
                    """,
                    request.trip_ids,
                )

        try:
            marked_count = int(result.split()[-1])
        except (ValueError, IndexError, AttributeError):
            marked_count = 0

        return {
            "status":    "success",
            "marked":    marked_count,
            "requested": len(request.trip_ids),
            "trip_ids":  request.trip_ids,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/trips/{trip_id}/mark-synced", response_model=MarkSyncedResponse)
async def mark_trip_synced(
    trip_id: int,
    body: MarkSyncedRequest = MarkSyncedRequest(),
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    try:
        existing = await pool.fetchrow(
            "SELECT id, synced_to_odoo, synced_at FROM trip_logs WHERE id = $1",
            trip_id,
        )

        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"ไม่พบ trip id={trip_id} ในระบบ",
            )

        if existing["synced_to_odoo"]:
            return MarkSyncedResponse(
                status="already_synced",
                trip_id=trip_id,
                synced_to_odoo=True,
                synced_at=existing["synced_at"],
            )

        synced_at_value = body.synced_at or datetime.utcnow()

        updated = await pool.fetchrow(
            """
            UPDATE trip_logs
            SET synced_to_odoo = true,
                synced_at      = $2
            WHERE id = $1
            RETURNING id, synced_to_odoo, synced_at
            """,
            trip_id,
            synced_at_value,
        )

        return MarkSyncedResponse(
            status="success",
            trip_id=trip_id,
            synced_to_odoo=updated["synced_to_odoo"],
            synced_at=updated["synced_at"],
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/trips/{trip_id}",
    summary="รายละเอียด trip + GPS track + harsh events",
    tags=["Trips"],
    responses={
        200: {"description": "ข้อมูล trip ครบถ้วนพร้อม GPS track และ events"},
        404: {"description": "ไม่พบ trip นี้"},
        500: {"description": "Database error"},
    },
)
async def get_trip_detail(
    trip_id: int,
    include_gps_track: bool = Query(
        default=True,
        description="true = ส่ง GPS track array มาด้วย (อาจหนักถ้า trip ยาว), false = ส่งแค่ summary",
    ),
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    try:
        trip = await pool.fetchrow(
            """
            SELECT
                id, device_id, vehicle_id, driver_id,
                trip_start, trip_end,
                distance_km, duration_min, idle_min,
                max_speed, avg_speed,
                harsh_brake_count, harsh_accel_count,
                harsh_corner_count, speeding_count,
                driver_score, fuel_used,
                gps_track,
                synced_to_odoo, synced_at,
                created_at
            FROM trip_logs
            WHERE id = $1
            """,
            trip_id,
        )

        if not trip:
            raise HTTPException(
                status_code=404,
                detail=f"ไม่พบ trip id={trip_id} ในระบบ",
            )

        result = dict(trip)

        if not include_gps_track:
            result.pop("gps_track", None)

        events = []
        if trip["trip_start"] and trip["device_id"]:
            trip_end_filter = trip["trip_end"] or datetime.utcnow()

            raw_events = await pool.fetch(
                """
                SELECT
                    ts, lat, lon, speed,
                    event, event_severity,
                    ax, ay, az
                FROM telemetry_raw
                WHERE device_id = $1
                  AND ts BETWEEN $2 AND $3
                  AND event IS NOT NULL
                  AND event != ''
                ORDER BY ts ASC
                """,
                trip["device_id"],
                trip["trip_start"],
                trip_end_filter,
            )
            events = [dict(e) for e in raw_events]

        result["events"]      = events
        result["event_count"] = len(events)

        score = float(trip["driver_score"] or 0)
        if score >= 90:
            tier = "A"
        elif score >= 75:
            tier = "B"
        elif score >= 60:
            tier = "C"
        else:
            tier = "D"

        result["incentive_tier"] = tier

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
