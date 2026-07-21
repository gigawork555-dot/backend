# app/api/routes_trips.py
#
# 🔴 CRITICAL FIX (previous revision): Add APIKEY authentication to EVERY
#     endpoint in this file. FDD v1.4 §13 Security requires:
#         "Authentication: JWT token สำหรับ API, MQTT username/password
#          per device"
#     Before that fix, routes_trips.py had ZERO auth on any endpoint —
#     anyone could pull all trip logs (driver GPS/behavior data) via
#     POST /webhook/odoo-sync, or mark trips as synced via
#     PATCH /trips/{id}/mark-synced without Odoo ever having received
#     them — causing silent permanent data loss (Odoo's cron only pulls
#     rows where synced_to_odoo=false, so a maliciously/accidentally
#     marked-synced trip is never retried). Pattern mirrors
#     routes_vehicles.py / routes_drivers.py / routes_reports.py
#     (APIKeyHeader "APIKEY" + _verify_api_key dependency).
#
# 🔴 CRITICAL FIX (this revision): This file previously had the
#     `@router.patch("/trips/batch/mark-synced", ...)` route/function
#     declared TWICE (an accidental copy-paste). FastAPI registers
#     routes in declaration order and matches top-down, so every
#     request to /trips/batch/mark-synced was always served by the
#     FIRST (buggy, older) definition — which reported
#     `marked = len(trip_ids)` unconditionally instead of the actual
#     UPDATE rowcount, and had no `requested` field in its response.
#     The second (correct, parses "UPDATE n" from conn.execute()) copy
#     was dead code, unreachable.
#
#     Worse: because the duplicate consumed the "batch/mark-synced"
#     block, the single-trip endpoint `PATCH /trips/{trip_id}/mark-synced`
#     — which the module docstring/comments below still reference —
#     had been entirely DELETED from this file. Any request to
#     /trips/{id}/mark-synced fell through to no matching route and
#     FastAPI/Starlette returned 404.
#
#     Fix: removed the duplicate (buggy) batch route entirely, kept
#     the single correct batch route (parses actual UPDATE rowcount,
#     includes "requested" in the response), and re-added the missing
#     single-trip PATCH /trips/{trip_id}/mark-synced endpoint with
#     idempotent "already_synced" handling.

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import asyncpg

from app.database import get_db_pool

router = APIRouter(prefix="/api/v1", tags=["Trips"])

# ─────────────────────────────────────────────────────────────
# API Key auth (FIX — FDD §13)
# ─────────────────────────────────────────────────────────────
# ใช้ค่าเดียวกับ routes_vehicles.py / routes_drivers.py / routes_reports.py /
# routes_config.py เพื่อความสอดคล้องกันทั้งระบบในตอนนี้ — งานถัดไปที่ควรทำ
# (ไม่ใช่ scope ของ fix นี้): แยก scope เฉพาะสำหรับ endpoint ที่ Odoo เรียก
# โดยใช้ verify_odoo_api_key() ที่มีอยู่แล้วใน app/auth/dependencies.py
API_KEY = "ktc-fleet-2026-secret"
api_key_header = APIKeyHeader(name="APIKEY", auto_error=False)


async def _verify_api_key(api_key: str = Security(api_key_header)) -> str:
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="API Key ไม่ถูกต้อง")
    return api_key


# ─────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────

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
    """
    Request body for POST /api/v1/webhook/odoo-sync (FDD §11.3).

    last_sync_timestamp is optional — if omitted, all unsynced trips
    are returned (subject to the 200-record cap). Odoo is expected to
    persist the `last_sync_timestamp` returned in the response and pass
    it back on the next call as a cursor.
    """
    last_sync_timestamp: Optional[datetime] = None


# ─────────────────────────────────────────────────────────────
# POST /api/v1/webhook/odoo-sync
#
# FDD v1.4 §11.3 — Config Sync (Odoo → Backend) group:
#   "POST /api/v1/webhook/odoo-sync — Odoo pull trip logs ที่ยังไม่
#    sync (batch ≤ 200 records)"
#
# ⚠️ Path นี้อยู่คนละ segment แรกกับ "/trips/..." (คือ "/webhook/...")
#    จึงไม่ชนกับ /trips/unsynced, /trips/batch/mark-synced หรือ
#    /trips/{trip_id} ไม่ว่าจะประกาศไว้ตำแหน่งไหนในไฟล์นี้ก็ตาม
#    (FastAPI จับคู่ตาม path prefix แบบเต็มเส้นทาง ไม่ใช่แค่ prefix
#    เดียวกับ router ที่ join กันไว้ตอน APIRouter(prefix="/api/v1"))
#
# Endpoint นี้เป็นคนละตัวกับ GET /api/v1/trips/unsynced ที่มีอยู่เดิม:
#   - GET /trips/unsynced   → general-purpose polling, filter ได้หลาย
#                             field (vehicle_id/device_id/driver_id/
#                             since/last_id), ไม่มี cap 200 ตายตัว
#   - POST /webhook/odoo-sync → ตรงตาม FDD §11.3 เป๊ะๆ, รับ
#                             last_sync_timestamp เป็น cursor,
#                             cap 200 records ต่อครั้งเสมอ
# ทั้งสอง endpoint คงอยู่คู่กันได้ ไม่ได้แทนที่กัน
# ─────────────────────────────────────────────────────────────

@router.post(
    "/webhook/odoo-sync",
    summary="[FDD §11.3] Odoo pull trip logs ที่ยังไม่ sync (batch ≤ 200 records)",
    tags=["Trips"],
)
async def odoo_sync_webhook(
    request: OdooSyncWebhookRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX]
):
    """
    FDD v1.4 §11.3 — Config Sync (Odoo → Backend):

        "POST /api/v1/webhook/odoo-sync — Odoo pull trip logs ที่ยังไม่
         sync (batch ≤ 200 records)"

    **Authentication:** ต้องใส่ APIKEY header (FDD §13) — endpoint นี้
    ส่งข้อมูล GPS/พฤติกรรมพนักงานทั้งหมดออกไป จึงต้องป้องกันไม่ให้ใครก็
    ดึงออกไปได้

    Odoo เรียก endpoint นี้ (ปกติทุก 5 นาที ตาม cron §12.5) พร้อม
    last_sync_timestamp ของรอบก่อนหน้า เพื่อดึง trip_logs ใหม่ที่ยัง
    ไม่ sync เท่านั้น — ไม่ต้อง query Odoo ทุก record ซ้ำ

    Request Body:
        last_sync_timestamp (optional, ISO 8601) — ถ้าระบุ จะกรองเฉพาะ
        trip ที่ created_at > last_sync_timestamp เท่านั้น (นอกเหนือจาก
        synced_to_odoo = false อยู่แล้ว) ถ้าไม่ระบุ จะดึง trip ที่ยัง
        ไม่ sync ทั้งหมด (จำกัดด้วย cap 200 อยู่ดี)

    Response:
        total                — จำนวน trip ที่ส่งกลับในรอบนี้ (≤ 200)
        last_sync_timestamp  — เวลาปัจจุบันของ server ณ ตอน query
                                (Odoo ควรเก็บค่านี้ไว้ใช้เป็น cursor
                                ของรอบถัดไป แทนการคำนวณเวลาเอง)
        trips                — array ของ trip_logs ที่ยังไม่ sync

    หมายเหตุ: endpoint นี้เป็น read-only ไม่ mark synced ให้อัตโนมัติ —
    หลัง Odoo import สำเร็จ ต้องเรียก PATCH /api/v1/trips/{id}/mark-synced
    หรือ PATCH /api/v1/trips/batch/mark-synced ต่อ ตาม flow เดิม
    """
    try:
        where_clauses = ["synced_to_odoo = false"]
        params: list = []

        if request.last_sync_timestamp is not None:
            params.append(request.last_sync_timestamp)
            where_clauses.append(f"created_at > ${len(params)}")

        where_sql = " AND ".join(where_clauses)

        # เวลาที่ query จริง — ใช้เป็น cursor รอบถัดไปให้ Odoo
        # (จับก่อน query เพื่อไม่พลาด trip ที่ insert แทรกระหว่างอ่าน)
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


# ─────────────────────────────────────────────────────────────
# GET /api/v1/trips/unsynced
# ดึง trip ที่ยังไม่ sync — Odoo cron เรียกทุก 5 นาที
#
# ⚠️ ต้องอยู่ก่อน /{trip_id} เพราะ FastAPI match route จากบนลงล่าง
#    ถ้า {trip_id} อยู่ก่อน FastAPI จะ parse "unsynced" เป็น trip_id
#    แล้วส่ง 422 int_parsing error
# ─────────────────────────────────────────────────────────────

@router.get("/trips/unsynced")
async def get_unsynced_trips(
    vehicle_id: Optional[int]      = None,
    device_id:  Optional[str]      = None,
    driver_id:  Optional[int]      = None,
    since:      Optional[datetime] = None,
    last_id:    Optional[int]      = None,
    limit:      int                = 100,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX]
):
    """
    ดึงรายการ trip ที่ยังไม่ได้ sync ไป Odoo

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    Query Parameters:
        vehicle_id : กรองตามรถ (optional)
        device_id  : กรองตามบอร์ด (optional)
        driver_id  : กรองตามคนขับ (optional)
        limit      : จำนวนสูงสุด (default 100)

    ป้องกันดึงซ้ำ: กรอง synced_to_odoo = false เท่านั้น
    หลัง Odoo import เสร็จให้เรียก PATCH /trips/{id}/mark-synced
    """
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


# ─────────────────────────────────────────────────────────────
# PATCH /api/v1/trips/batch/mark-synced
#
# ⚠️ ต้องอยู่ก่อน /{trip_id}/mark-synced
#    เพื่อไม่ให้ "batch" ถูก match เป็น trip_id
#
# 🔴 [FIX — this revision] เดิมมี route/function นี้ประกาศซ้ำกัน 2
#    ครั้งในไฟล์นี้ (accidental copy-paste) — FastAPI จะ match ตัวแรก
#    (ตัวเก่า/บั๊ก) เสมอเพราะลงทะเบียนตามลำดับบนลงล่าง ทำให้ตัวที่สอง
#    (ตัวที่แก้ให้ parse UPDATE rowcount จริงแล้ว) กลายเป็น dead code
#    ไม่เคยถูกเรียกใช้จริงเลย
#
#    แก้ไข: เหลือ route นี้ไว้ตัวเดียว — เป็นเวอร์ชันที่ถูกต้อง
#    (parse ผลลัพธ์จริงจาก conn.execute(), มี "requested" ในผลลัพธ์)
# ─────────────────────────────────────────────────────────────

@router.patch("/trips/batch/mark-synced", status_code=200)
async def mark_trips_synced_batch(
    request: BatchMarkSyncedRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),
):
    """
    Mark หลาย trip ว่า sync แล้วพร้อมกัน (All-or-Nothing transaction)

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    Request Body: { "trip_ids": [10, 11, 12] }

    [FIX #3] response field "marked" เดิมคืน len(trip_ids) ของ input
    เสมอ ไม่ว่า UPDATE จะสำเร็จกี่แถวจริง — ถ้ามี id ที่ไม่มีในระบบ
    หรือ sync ไปแล้ว (synced_to_odoo=true อยู่ก่อน) จำนวนแถวที่ update
    จริงจะน้อยกว่า len(trip_ids) เสมอ ทำให้ Odoo เข้าใจผิดว่า sync
    ครบแล้วทั้งที่ยังไม่ครบ กระทบ FDD §13 "Incentive cron reliability:
    สร้างสำเร็จ 100% ของพนักงานที่มี trip ในรอบ" — trip ที่ไม่ถูก
    update จริงจะไม่ถูก sync ซ้ำอีก (Odoo cron query เฉพาะ
    synced_to_odoo=false) ทำให้ trip หายจากการคำนวณ incentive เงียบๆ

    แก้ไข: parse ผลลัพธ์จริงจาก conn.execute() ซึ่ง asyncpg คืนเป็น
    string รูปแบบ "UPDATE <n>" เอาตัวเลข <n> มาใช้แทน len(trip_ids)
    """
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

        # [FIX #3] asyncpg execute() คืน string เช่น "UPDATE 2" —
        # parse เอาตัวเลขจริงมาใช้ ถ้า parse ไม่ได้ (รูปแบบผิดคาด)
        # fallback เป็น 0 เพื่อไม่ให้ raise แล้วรายงานผิดพลาดแบบเงียบ
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


# ─────────────────────────────────────────────────────────────
# PATCH /api/v1/trips/{trip_id}/mark-synced
#
# 🔴 [FIX — this revision] endpoint นี้เคยหายไปจากไฟล์ทั้งหมด เพราะ
#    ถูกเขียนทับด้วย mark_trips_synced_batch() ที่ประกาศซ้ำ (ดู
#    คำอธิบายด้านบนที่ header ของไฟล์) ทำให้ทุก request ไปที่
#    /trips/{trip_id}/mark-synced ได้ 404 เสมอ (ไม่มี route ให้ match)
#
#    แก้ไข: เพิ่ม endpoint นี้กลับเข้ามา — mark trip เดี่ยวว่า sync
#    แล้ว, idempotent (เรียกซ้ำกับ trip ที่ sync ไปแล้วไม่ error แต่
#    คืน status="already_synced" แทน)
#
# ⚠️ ต้องอยู่ "หลัง" /trips/batch/mark-synced เพื่อไม่ให้ "batch"
#    ถูก parse เป็น {trip_id} (FastAPI match แบบ top-down)
# ─────────────────────────────────────────────────────────────

@router.patch("/trips/{trip_id}/mark-synced", response_model=MarkSyncedResponse)
async def mark_trip_synced(
    trip_id: int,
    body: MarkSyncedRequest = MarkSyncedRequest(),
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),
):
    """
    Mark trip เดี่ยวว่า sync ไป Odoo แล้ว

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    Request Body (optional):
        synced_at — เวลาที่ sync สำเร็จ ถ้าไม่ระบุจะใช้เวลาปัจจุบัน
                    ของ server (UTC)

    **Idempotent:** ถ้า trip นี้ synced_to_odoo=true อยู่แล้ว จะคืน
    status="already_synced" พร้อมค่า synced_at เดิม แทนที่จะ error
    หรือ update ซ้ำ

    Errors:
        404 — ไม่พบ trip_id นี้ในระบบ
        500 — Database error
    """
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

        # ── Idempotent: ถ้า sync ไปแล้ว ไม่ต้อง update ซ้ำ ──────
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


# ─────────────────────────────────────────────────────────────
# GET /api/v1/trips/{trip_id}
# รายละเอียด trip เดี่ยว + GPS track + events
#
# ⚠️ ต้องอยู่หลังสุด เพราะ {trip_id} จะ match ทุก string
#    ถ้าอยู่ก่อน route อื่น จะกิน "unsynced" และ "batch" ไปด้วย
# ─────────────────────────────────────────────────────────────

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
    api_key: str = Security(_verify_api_key),  # [FIX]
):
    """
    ดึงรายละเอียด trip เดี่ยวตาม trip_id

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    **Response ประกอบด้วย:**
    - ข้อมูลสรุป trip (ระยะทาง, เวลา, คะแนน, idling)
    - จำนวน harsh events แยกประเภท
    - `gps_track` — array จุด GPS ตลอดเส้นทาง (ถ้า include_gps_track=true)
    - `events` — รายการ harsh events ดึงจาก telemetry_raw

    **Query Parameters:**
    - `include_gps_track` (bool, default true) — ถ้า false จะไม่ส่ง gps_track มา ประหยัด bandwidth
    """
    try:
        # ── Step 1: ดึง trip summary จาก trip_logs ──────────────
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

        # ── Step 2: ซ่อน gps_track ถ้าไม่ต้องการ ───────────────
        if not include_gps_track:
            result.pop("gps_track", None)

        # ── Step 3: ดึง harsh events จาก telemetry_raw ──────────
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

        # ── Step 4: คำนวณ incentive tier จาก driver_score ───────
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