# app/api/routes_vehicles.py
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Security, Query
from fastapi.security import APIKeyHeader
from fastapi.responses import StreamingResponse
import asyncpg
import asyncio
import json
import logging
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/vehicles", tags=["Vehicle Monitoring"])

API_KEY = "ktc-fleet-2026-secret"
api_key_header = APIKeyHeader(name="APIKEY", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="API Key ไม่ถูกต้อง")
    return api_key


async def get_db_connection():
    try:
        return await asyncpg.connect(
            user=settings.DB_USER, password=settings.DB_PASS,
            database=settings.DB_NAME, host=settings.DB_HOST, port=settings.DB_PORT
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"เชื่อมต่อฐานข้อมูลล้มเหลว: {str(e)}")


# ============================================================
# GET /api/v1/vehicles — รายการรถทั้งหมด
# ============================================================
@router.get("", summary="ดูรายการรถทั้งหมดพร้อมสถานะและตำแหน่ง")
async def get_all_vehicles(api_key: str = Security(verify_api_key)):
    """ดึงรายการรถทั้งหมดพร้อม device ที่ผูกอยู่และ telemetry ล่าสุด"""
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT
                us.vehicle_id,
                us.device_id,
                us.driver_id,
                us.date_update_latest,
                d.active,
                t.lat, t.lon, t.speed, t.ignition, t.ts AS last_seen
            FROM update_status us
            LEFT JOIN devices d ON d.id = us.device_id
            LEFT JOIN LATERAL (
                SELECT lat, lon, speed, ignition, ts
                FROM telemetry_raw
                WHERE device_id = us.device_id
                ORDER BY ts DESC LIMIT 1
            ) t ON true
            ORDER BY us.vehicle_id ASC
        """)
        return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# ============================================================
# [API เส้นที่ 1] GET /api/v1/vehicles/{vehicle_id}/device
# ข้อมูล device ที่ผูกกับรถ + สถานะ Odoo sync
# ============================================================
@router.get(
    "/{vehicle_id}/device",
    summary="[Odoo/หน้าบ้าน] ดูข้อมูล device ที่ผูกกับรถ + วันที่อัปเดตล่าสุด",
)
async def get_vehicle_device(
    vehicle_id: int,
    api_key: str = Security(verify_api_key),
):
    """
    API เส้นที่ 1 — ข้อมูลความสัมพันธ์ รถ ↔ บอร์ด

    ใช้สำหรับ:
    - Odoo ตรวจสอบว่า vehicle_id ผูกกับบอร์ดใดอยู่
    - หน้าบ้านแสดงสถานะบอร์ดของรถ
    - ESP32 ตรวจสอบว่าตัวเองผูกกับรถคันไหน

    Response:
    - vehicle_id: รหัสรถ
    - device_id: รหัสบอร์ด ESP32
    - active: บอร์ดเปิดใช้งานอยู่หรือไม่
    - date_update_latest: วันที่ Odoo อัปเดตล่าสุด
    - has_telemetry: มีข้อมูลจากบอร์ดนี้หรือไม่
    """
    conn = await get_db_connection()
    try:
        row = await conn.fetchrow("""
            SELECT
                us.vehicle_id,
                us.device_id,
                us.driver_id,
                d.active,
                d.firmware_ver,
                us.date_update_latest,
                EXISTS (
                    SELECT 1 FROM telemetry_raw t
                    WHERE t.device_id = us.device_id
                    LIMIT 1
                ) AS has_telemetry
            FROM update_status us
            LEFT JOIN devices d ON d.id = us.device_id
            WHERE us.vehicle_id = $1
            LIMIT 1
        """, vehicle_id)

        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"ไม่พบรถ vehicle_id={vehicle_id} ในระบบ หรือยังไม่ได้ผูกบอร์ด"
            )

        logger.info(f"[vehicles/device] OK vehicle={vehicle_id} device={row['device_id']} driver={row['driver_id']}")
        return {
            "vehicle_id": row["vehicle_id"],
            "device_id": row["device_id"],
            "driver_id": row["driver_id"],
            "active": row["active"],
            "firmware_ver": row["firmware_ver"],
            "date_update_latest": row["date_update_latest"],
            "has_telemetry": row["has_telemetry"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[vehicles/device] ERROR vehicle={vehicle_id} | {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# ============================================================
# [API เส้นที่ 2] GET /api/v1/vehicles/{vehicle_id}/location
# Location ล่าสุดของรถ
# ============================================================
@router.get(
    "/{vehicle_id}/location",
    summary="[Odoo/หน้าบ้าน] ดู location ล่าสุดของรถ",
)
async def get_vehicle_location(
    vehicle_id: int,
    api_key: str = Security(verify_api_key),
):
    """
    API เส้นที่ 2 — ตำแหน่ง GPS ล่าสุดของรถ

    ใช้สำหรับ:
    - หน้าบ้านแสดงตำแหน่งรถบนแผนที่
    - Odoo ดูสถานะรถ (ignition, speed, position)

    Flow: vehicle_id → ค้นหา active device → ดึง telemetry ล่าสุด

    Response:
    - vehicle_id, device_id
    - ts: เวลาข้อมูลล่าสุด
    - lat, lon: พิกัด GPS
    - speed: ความเร็ว (km/h)
    - heading: ทิศทาง (องศา)
    - ignition: สถานะกุญแจ
    - event: เหตุการณ์ล่าสุด (harsh_brake, speeding ฯลฯ)
    """
    conn = await get_db_connection()
    try:
        # ค้นหา active device ที่ผูกกับรถคันนี้
        device = await conn.fetchrow(
            "SELECT id FROM devices WHERE vehicle_id = $1 AND active = true LIMIT 1",
            vehicle_id
        )
        if not device:
            logger.warning(f"[vehicles/location] NOT_FOUND vehicle={vehicle_id} — ไม่มี device active")
            raise HTTPException(
                status_code=404,
                detail=f"ไม่พบอุปกรณ์ที่ผูกกับรถ vehicle_id={vehicle_id} หรือบอร์ดไม่ได้ active"
            )
        device_id = device["id"]

        # ดึง telemetry ล่าสุด
        row = await conn.fetchrow("""
            SELECT ts, lat, lon, speed, heading, ignition, event
            FROM telemetry_raw
            WHERE device_id = $1
            ORDER BY ts DESC LIMIT 1
        """, device_id)

        if not row:
            logger.warning(f"[vehicles/location] NO_TELEMETRY vehicle={vehicle_id} device={device_id}")
            raise HTTPException(
                status_code=404,
                detail=f"ยังไม่มีข้อมูล telemetry จากบอร์ด {device_id}"
            )

        logger.info(
            f"[vehicles/location] OK vehicle={vehicle_id} device={device_id} "
            f"lat={row['lat']} lon={row['lon']} speed={row['speed']} ignition={row['ignition']}"
        )
        return {
            "vehicle_id": vehicle_id,
            "device_id": device_id,
            "ts": row["ts"],
            "lat": row["lat"],
            "lon": row["lon"],
            "speed": row["speed"],
            "heading": row["heading"],
            "ignition": row["ignition"],
            "event": row["event"] or None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[vehicles/location] ERROR vehicle={vehicle_id} | {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# ============================================================
# GET /api/v1/vehicles/{vehicle_id}/trips — ประวัติ trip
# ============================================================
@router.get(
    "/{vehicle_id}/trips",
    summary="ดูประวัติ trip ของรถตาม vehicle_id พร้อม pagination และ filter",
)
async def get_vehicle_trips(
    vehicle_id: int,
    page: int = Query(
        default=1,
        ge=1,
        description="หน้าที่ต้องการ (เริ่มที่ 1)",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=200,
        description="จำนวน trip ต่อหน้า (สูงสุด 200)",
    ),
    date_from: Optional[datetime] = Query(
        default=None,
        description="กรองวันเริ่มต้น เช่น 2026-01-01T00:00:00 (ISO 8601)",
    ),
    date_to: Optional[datetime] = Query(
        default=None,
        description="กรองวันสิ้นสุด เช่น 2026-06-30T23:59:59 (ISO 8601)",
    ),
    synced_only: bool = Query(
        default=False,
        description="true = เฉพาะ trip ที่ sync ไป Odoo แล้ว, false = ทั้งหมด",
    ),
    api_key: str = Security(verify_api_key),
):
    """
    ดึงประวัติ trip ของรถคันนี้ พร้อม pagination และ filter

    **Path Parameter:**
    - `vehicle_id` — รหัสรถ (integer) เช่น 1

    **Query Parameters:**
    - `page` — หน้าที่ต้องการ (default 1)
    - `limit` — จำนวน trip ต่อหน้า (default 20, สูงสุด 200)
    - `date_from` — กรองเฉพาะ trip ที่เริ่มหลังวันนี้ (ISO 8601)
    - `date_to` — กรองเฉพาะ trip ที่เริ่มก่อนวันนี้ (ISO 8601)
    - `synced_only` — true = เฉพาะที่ sync Odoo แล้ว

    **Response:**
    - `total` — จำนวน trip ทั้งหมดที่ตรงเงื่อนไข
    - `page`, `limit`, `total_pages` — ข้อมูล pagination
    - `trips` — รายการ trip ในหน้านี้
    """
    try:
        conn = await get_db_connection()

        # ── สร้าง WHERE clause แบบ dynamic ──────────────────────
        where_clauses = ["vehicle_id = $1"]
        params: list = [vehicle_id]

        if date_from is not None:
            params.append(date_from)
            where_clauses.append(f"trip_start >= ${len(params)}")

        if date_to is not None:
            params.append(date_to)
            where_clauses.append(f"trip_start <= ${len(params)}")

        if synced_only:
            where_clauses.append("synced_to_odoo = true")

        where_sql = " AND ".join(where_clauses)

        # ── นับจำนวนทั้งหมดสำหรับ pagination ───────────────────
        total: int = await conn.fetchval(
            f"SELECT COUNT(*) FROM trip_logs WHERE {where_sql}",
            *params,
        )

        # ── คำนวณ offset ────────────────────────────────────────
        offset = (page - 1) * limit
        total_pages = max(1, -(-total // limit))  # ceiling division

        # ── ดึงข้อมูลหน้านี้ ─────────────────────────────────────
        params_with_pagination = params + [limit, offset]
        limit_param  = len(params_with_pagination) - 1
        offset_param = len(params_with_pagination)

        rows = await conn.fetch(
            f"""
            SELECT
                id, device_id, vehicle_id, driver_id,
                trip_start, trip_end,
                distance_km, duration_min, idle_min,
                max_speed, avg_speed,
                harsh_brake_count, harsh_accel_count,
                harsh_corner_count, speeding_count,
                driver_score, fuel_used,
                synced_to_odoo, synced_at,
                created_at
            FROM trip_logs
            WHERE {where_sql}
            ORDER BY trip_start DESC
            LIMIT ${limit_param} OFFSET ${offset_param}
            """,
            *params_with_pagination,
        )

        await conn.close()

        return {
            "vehicle_id":  vehicle_id,
            "page":        page,
            "limit":       limit,
            "total":       total,
            "total_pages": total_pages,
            "filters": {
                "date_from":   date_from.isoformat() if date_from else None,
                "date_to":     date_to.isoformat()   if date_to   else None,
                "synced_only": synced_only,
            },
            "trips": [dict(r) for r in rows],
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# GET /api/v1/fleet/live — SSE real-time
# ============================================================
fleet_router = APIRouter(prefix="/api/v1/fleet", tags=["Fleet Live"])


@fleet_router.get("/live", summary="SSE real-time ตำแหน่งรถทุกคัน ส่งทุก 5 วินาที")
async def fleet_live(api_key: str = Security(api_key_header)):
    """Server-Sent Events stream — ข้อมูลทุก 5 วินาที (Swagger จะหมุนตลอด ปกติของ SSE)"""
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="API Key ไม่ถูกต้อง")

    async def event_generator():
        while True:
            try:
                conn = await asyncpg.connect(
                    user=settings.DB_USER, password=settings.DB_PASS,
                    database=settings.DB_NAME, host=settings.DB_HOST, port=settings.DB_PORT
                )
                rows = await conn.fetch("""
                    SELECT us.vehicle_id, us.device_id,
                           t.lat, t.lon, t.speed, t.ignition, t.ts
                    FROM update_status us
                    LEFT JOIN LATERAL (
                        SELECT lat, lon, speed, ignition, ts
                        FROM telemetry_raw WHERE device_id = us.device_id
                        ORDER BY ts DESC LIMIT 1
                    ) t ON true
                    ORDER BY us.vehicle_id
                """)
                await conn.close()
                data = json.dumps([dict(r) for r in rows], default=str)
                yield f"data: {data}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )