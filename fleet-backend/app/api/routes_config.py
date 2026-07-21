# app/api/routes_config.py — FIXED VERSION
# 🔴 CRITICAL FIX #1: Add 409 Conflict validation for device-vehicle binding
# 🔴 CRITICAL FIX #2 (14:42): Add device_id format validation (KTC-XXX)
#     ป้องกันกรณีที่มีคน/ระบบส่ง device_id ผิด format เช่น "1" เข้ามา
#     แล้วไป bind กับ vehicle ทำให้ lookup_vehicle_id() ใน mqtt_subscriber.py
#     หา device_id ไม่เจอ (เพราะ ESP32 ส่งมาเป็น "KTC-001" จริง) → vehicle_id=None
#     → trip/event processing ถูกข้ามทั้งหมด
# 🔴 CRITICAL FIX #3 (this revision): Add APIKEY authentication to EVERY
#     endpoint in this file. FDD v1.4 §13 Security requires:
#         "Authentication: JWT token สำหรับ API, MQTT username/password
#          per device"
#     Before this fix, routes_config.py had ZERO auth on any endpoint —
#     anyone could push a fake scoring config (affects driver bonuses,
#     FDD §12.4) or rebind device<->vehicle<->driver (corrupts trip
#     attribution) with no credentials at all. Pattern mirrors
#     routes_vehicles.py / routes_drivers.py / routes_reports.py
#     (APIKeyHeader "APIKEY" + _verify_api_key dependency).

"""
Device Configuration & Management Endpoints

Handles:
- Device registration (single + batch)
- Device-to-vehicle binding with conflict prevention
- Vehicle config updates with device migration
- Scoring config (push from Odoo)
"""

import re
from fastapi import APIRouter, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, field_validator
import asyncpg
from typing import List, Optional
from datetime import datetime

from app.database import get_db_pool

router = APIRouter(prefix="/api/v1", tags=["Config"])

# ─────────────────────────────────────────────────────────────
# API Key auth (FIX #3 — FDD §13)
# ─────────────────────────────────────────────────────────────
# ใช้ค่าเดียวกับ routes_vehicles.py / routes_drivers.py / routes_reports.py
# เพื่อความสอดคล้องกันทั้งระบบในตอนนี้ — งานถัดไปที่ควรทำ (ไม่ใช่ scope
# ของ fix นี้): แยก scope เฉพาะสำหรับ endpoint ที่ Odoo เรียก
# (PUT /config/vehicle, POST /config/scoring) ออกจาก endpoint ที่ ESP32
# เรียก โดยใช้ verify_odoo_api_key() ที่มีอยู่แล้วใน app/auth/dependencies.py
API_KEY = "ktc-fleet-2026-secret"
api_key_header = APIKeyHeader(name="APIKEY", auto_error=False)


async def _verify_api_key(api_key: str = Security(api_key_header)) -> str:
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="API Key ไม่ถูกต้อง")
    return api_key


# ─────────────────────────────────────────────────────────────
# Device ID Format Validation
# ─────────────────────────────────────────────────────────────
# รูปแบบมาตรฐานตาม FDD v1.4 / mock_hardware_stream.py / ESP32 firmware
# ตัวอย่างที่ถูกต้อง: KTC-001, KTC-002, KTC-099, KTC-123
DEVICE_ID_PATTERN = re.compile(r"^KTC-\d{3}$")


def _validate_device_id_format(v: str, field_name: str = "device_id") -> str:
    """
    ตรวจสอบและ normalize device_id ให้ตรง format KTC-XXX เสมอ

    ป้องกัน:
    - device_id ที่เป็นตัวเลขล้วน เช่น "1" (สาเหตุของ bug binding ผิดที่เคยเกิด)
    - device_id ที่พิมพ์ผิด case หรือมีช่องว่างเกิน
    - device_id ที่ความยาวไม่ตรง (ต้องเป็น KTC- ตามด้วยเลข 3 หลัก)
    """
    if v is None:
        return v

    cleaned = v.strip().upper()

    if not DEVICE_ID_PATTERN.match(cleaned):
        raise ValueError(
            f"{field_name} ต้องเป็นรูปแบบ KTC-XXX เท่านั้น "
            f"(เช่น KTC-001, KTC-002) — ได้รับค่า: '{v}'"
        )

    return cleaned


# ─────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────

class RegisterDeviceRequest(BaseModel):
    """Request body for device registration"""
    device_id: str
    device_name: str
    vehicle_id: int

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, v: str) -> str:
        return _validate_device_id_format(v, "device_id")


class RegisterDeviceBatchRequest(BaseModel):
    """Request body for batch registration"""
    devices: List[RegisterDeviceRequest]


class VehicleConfigUpdate(BaseModel):
    """Update vehicle with new device (device migration)"""
    vehicle_id: int
    new_device_id: str
    old_device_id: Optional[str] = None  # Explicitly provide to ensure
    driver_id: Optional[int] = None      # รหัสคนขับ (ดึงจาก Odoo)

    @field_validator("new_device_id")
    @classmethod
    def validate_new_device_id(cls, v: str) -> str:
        return _validate_device_id_format(v, "new_device_id")

    @field_validator("old_device_id")
    @classmethod
    def validate_old_device_id(cls, v: Optional[str]) -> Optional[str]:
        # old_device_id เป็น optional — ถ้าไม่ส่งมาก็ไม่ต้อง validate
        if v is None or v == "":
            return None
        return _validate_device_id_format(v, "old_device_id")


class ScoringConfigRequest(BaseModel):
    """Scoring config pushed from Odoo"""
    config_name: str
    score_base: float = 100.0
    harsh_brake_deduct: float = 5.0
    harsh_accel_deduct: float = 3.0
    harsh_corner_deduct: float = 3.0
    speeding_deduct: float = 10.0
    idling_deduct: float = 2.0
    bump_deduct: float = 4.0
    harsh_brake_g: float = 0.40
    harsh_accel_g: float = 0.40
    harsh_corner_g: float = 0.40
    speeding_kmh_over: float = 20.0
    idle_min_threshold: float = 5.0
    max_deduct_per_trip: float = 50.0
    is_active: bool = True
    synced_from_odoo_at: Optional[datetime] = None


# ─────────────────────────────────────────────────────────────
# Register Single Device — WITH CONFLICT PREVENTION ✅
# ─────────────────────────────────────────────────────────────

async def _register_single(
    conn: asyncpg.Connection,
    item: RegisterDeviceRequest
) -> dict:
    """
    Register single device-to-vehicle binding

    🔴 CRITICAL FIX:
    - Check if EXACT binding (device + vehicle) already exists → 409
    - Check if device already bound to DIFFERENT vehicle → 409
    - Enforce 1-to-1 relationship

    หมายเหตุ: device_id ผ่านการ validate format (KTC-XXX) มาแล้วจาก
    Pydantic model ตอนรับ request ดังนั้นไม่ต้อง .upper() ซ้ำที่นี่
    แต่ใส่ไว้เผื่อความปลอดภัย (defense in depth)

    Args:
        conn: Database connection
        item: RegisterDeviceRequest

    Returns:
        dict with status, device_id, vehicle_id

    Raises:
        HTTPException(409): If conflict detected
    """

    device_id = item.device_id.strip().upper()
    vehicle_id = item.vehicle_id

    # ─────────────────────────────────────────────
    # ✅ Step 1: Check exact binding already exists
    # ─────────────────────────────────────────────

    existing_same_binding = await conn.fetchrow(
        """
        SELECT vehicle_id FROM update_status 
        WHERE device_id = $1 AND vehicle_id = $2
        """,
        device_id, vehicle_id
    )

    if existing_same_binding:
        # 🔴 CONFLICT: Device already bound to THIS vehicle
        raise HTTPException(
            status_code=409,
            detail=(
                f"Device {device_id} is already bound to vehicle {vehicle_id}. "
                f"No changes made."
            )
        )

    # ─────────────────────────────────────────────
    # ✅ Step 2: Check if device bound to DIFFERENT vehicle
    # ─────────────────────────────────────────────

    existing_other_binding = await conn.fetchrow(
        """
        SELECT vehicle_id FROM update_status 
        WHERE device_id = $1 AND vehicle_id != $2
        """,
        device_id, vehicle_id
    )

    if existing_other_binding:
        # 🔴 CONFLICT: Device already bound to another vehicle
        other_vehicle_id = existing_other_binding['vehicle_id']
        raise HTTPException(
            status_code=409,
            detail=(
                f"Device {device_id} is already bound to vehicle {other_vehicle_id}. "
                f"Use PUT /config/vehicle to migrate."
            )
        )

    # ─────────────────────────────────────────────
    # ✅ Step 3: Check if vehicle already has device
    # ─────────────────────────────────────────────

    existing_vehicle_device = await conn.fetchrow(
        """
        SELECT device_id FROM update_status 
        WHERE vehicle_id = $1 AND device_id != $2
        """,
        vehicle_id, device_id
    )

    if existing_vehicle_device:
        # 🔴 CONFLICT: Vehicle already has different device (1-to-1 violation)
        other_device_id = existing_vehicle_device['device_id']
        raise HTTPException(
            status_code=409,
            detail=(
                f"Vehicle {vehicle_id} is already bound to device {other_device_id}. "
                f"Cannot bind to {device_id}. Use PUT /config/vehicle to replace."
            )
        )

    # ─────────────────────────────────────────────
    # ✅ Step 4: All checks passed — Register binding
    # ─────────────────────────────────────────────

    try:
        await conn.execute(
            """
            INSERT INTO devices (id, vehicle_id, active, registered_at)
            VALUES ($1, $2, true, NOW())
            ON CONFLICT (id) 
            DO UPDATE SET vehicle_id = $2, active = true
            """,
            device_id, vehicle_id
        )

        await conn.execute(
            """
            INSERT INTO update_status (vehicle_id, device_id, date_update_latest)
            VALUES ($1, $2, NOW())
            ON CONFLICT (vehicle_id, device_id) 
            DO UPDATE SET date_update_latest = NOW()
            """,
            vehicle_id, device_id
        )

        return {
            "status": "success",
            "device_id": device_id,
            "vehicle_id": vehicle_id,
            "registered_at": datetime.utcnow().isoformat()
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}"
        )


# ─────────────────────────────────────────────────────────────
# GET Devices — List all available devices
# ─────────────────────────────────────────────────────────────

@router.get("/devices")
async def get_devices(
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX #3]
):
    """
    List all devices

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    Returns:
        {
            "total": 50,
            "devices": [
                {
                    "id": "KTC-001",
                    "vehicle_id": 101,
                    "active": true,
                    "registered_at": "2026-01-15T10:00:00Z"
                },
                ...
            ]
        }
    """

    try:
        devices = await pool.fetch(
            """
            SELECT id, vehicle_id, active, registered_at
            FROM devices
            ORDER BY id ASC
            """
        )

        return {
            "total": len(devices),
            "devices": [dict(d) for d in devices]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────
# GET Device Config — Check device binding status
# ─────────────────────────────────────────────────────────────

@router.get("/config_device")
async def get_device_config(
    device_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX #3]
):
    """
    Get current binding status of a device

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    Query Params:
        device_id: Device ID (e.g., "KTC-001")

    Returns:
        {
            "device_id": "KTC-001",
            "vehicle_id": 101,
            "is_bound": true,
            "status": "active",
            "date_update_latest": "2026-06-14T15:30:00Z"
        }
    """

    # หมายเหตุ: endpoint นี้เป็น GET query param ไม่ใช่ Pydantic body
    # จึง validate format ตรงนี้แทน เพื่อกัน garbage lookup ด้วย
    try:
        device_id = _validate_device_id_format(device_id, "device_id")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        row = await pool.fetchrow(
            """
            SELECT 
                d.id as device_id,
                d.vehicle_id,
                d.active,
                u.date_update_latest
            FROM devices d
            LEFT JOIN update_status u ON d.id = u.device_id
            WHERE d.id = $1
            """,
            device_id
        )

        if not row:
            raise HTTPException(status_code=404, detail="Device not found")

        return {
            "device_id": row['device_id'],
            "vehicle_id": row['vehicle_id'],
            "is_bound": row['vehicle_id'] is not None,
            "status": "active" if row['active'] else "inactive",
            "date_update_latest": row['date_update_latest']
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────
# POST Register Single Device
# ─────────────────────────────────────────────────────────────

@router.post("/config_device/register", status_code=201)
async def register_device_single(
    request: RegisterDeviceRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX #3]
):
    """
    Register single device-to-vehicle binding

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    Request Body:
        {
            "device_id": "KTC-001",
            "device_name": "Device 1",
            "vehicle_id": 101
        }

    Returns:
        201 Created with binding details
        409 Conflict if duplicate/conflict detected
        422 Unprocessable Entity if device_id format ผิด (ไม่ใช่ KTC-XXX)

    Errors:
        - 404: Vehicle not found
        - 409: Duplicate binding or 1-to-1 violation
        - 422: device_id format ไม่ถูกต้อง
        - 500: Database error
    """

    try:
        async with pool.acquire() as conn:
            register_result = await _register_single(conn, request)
            return register_result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────
# POST Register Batch Devices — All-or-Nothing
# ─────────────────────────────────────────────────────────────

@router.post("/config_device/register/batch", status_code=201)
async def register_device_batch(
    request: RegisterDeviceBatchRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX #3]
):
    """
    Register multiple devices in batch (All-or-Nothing transaction)

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    Request Body:
        {
            "devices": [
                {"device_id": "KTC-001", "device_name": "Dev 1", "vehicle_id": 101},
                {"device_id": "KTC-002", "device_name": "Dev 2", "vehicle_id": 102},
                ...
            ]
        }

    Returns:
        201 Created with:
        {
            "status": "success",
            "registered": 2,
            "results": [
                {"device_id": "KTC-001", "vehicle_id": 101, "status": "success"},
                ...
            ]
        }

    Note:
        - ทุก device_id ใน list ถูก validate format (KTC-XXX) ตั้งแต่ตอนรับ
          request (Pydantic) — ถ้ามีตัวใดผิด format จะโดน 422 ทั้ง batch
          ก่อนแม้แต่จะเริ่ม transaction
        - ถ้ามี device ใด conflict ระหว่างประมวลผล ENTIRE transaction
          rolls back (all-or-nothing)
    """

    if not request.devices:
        raise HTTPException(status_code=400, detail="No devices provided")

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():  # ✅ All-or-Nothing

                results = []

                for item in request.devices:
                    try:
                        batch_item_result = await _register_single(conn, item)
                        results.append(batch_item_result)

                    except HTTPException as e:
                        # Re-raise to trigger rollback
                        raise

                return {
                    "status": "success",
                    "registered": len(results),
                    "results": results
                }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────
# PUT Update Vehicle Config — Device Bind / Migration
# ─────────────────────────────────────────────────────────────

@router.put("/config/vehicle")
async def update_vehicle_config(
    request: VehicleConfigUpdate,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX #3]
):
    """
    Odoo เรียกเมื่อผูกหรือเปลี่ยนบอร์ด ESP32 ให้รถ

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    รองรับ 3 กรณี:
    1. รถยังไม่มีบอร์ด → register ใหม่ทันที (ไม่ throw 404)
    2. รถมีบอร์ดเดิม = บอร์ดใหม่ → return no_change
    3. รถมีบอร์ดเดิม ≠ บอร์ดใหม่ → migrate แล้ว bind ใหม่
       - ถ้าบอร์ดใหม่ผูกกับรถอื่นอยู่ → ปลดออกก่อน (ไม่ throw 409)

    Body:
        vehicle_id   : int  — รหัสรถ
        new_device_id: str  — รหัสบอร์ดใหม่ (ต้องเป็นรูปแบบ KTC-XXX)
        old_device_id: str? — optional safety check (ต้องเป็นรูปแบบ KTC-XXX ถ้าส่งมา)

    Returns:
        status: "registered" | "no_change" | "driver_updated" | "migrated"

    Raises:
        422: ถ้า new_device_id หรือ old_device_id ไม่ตรงรูปแบบ KTC-XXX
             (กันไม่ให้เกิด binding ผิดแบบที่เคยเจอ เช่น device_id="1")
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():

                vehicle_id    = request.vehicle_id
                # device_id ผ่าน validation/normalize (.strip().upper()) จาก
                # Pydantic field_validator มาแล้ว ใช้ได้ตรงๆ
                new_device_id = request.new_device_id
                old_device_id = request.old_device_id

                # ── 1. หาบอร์ดและ driver ปัจจุบันของรถคันนี้ ────────────
                current = await conn.fetchrow(
                    "SELECT device_id, driver_id FROM update_status WHERE vehicle_id = $1 LIMIT 1",
                    vehicle_id
                )
                actual_old_device = current["device_id"] if current else None
                actual_old_driver = current["driver_id"] if current else None

                # ── safety check: old_device_id ที่ Odoo ส่งมาตรงกันไหม ──
                if old_device_id and actual_old_device and old_device_id != actual_old_device:
                    pass

                # ── 2. บอร์ดเดิม = บอร์ดใหม่ AND driver ไม่เปลี่ยน → no_change
                device_same   = actual_old_device and actual_old_device == new_device_id
                driver_same   = actual_old_driver == request.driver_id

                if device_same and driver_same:
                    return {
                        "status": "no_change",
                        "vehicle_id": vehicle_id,
                        "device_id": new_device_id,
                        "driver_id": request.driver_id,
                        "previous_device_id": None,
                        "migrated_trip_logs": 0,
                        "message": f"รถ {vehicle_id} ผูกกับบอร์ด {new_device_id} และคนขับ {request.driver_id} อยู่แล้ว"
                    }

                # ── 2b. บอร์ดเดิมแต่ driver เปลี่ยน → อัปเดต driver_id อย่างเดียว ──
                if device_same and not driver_same:
                    await conn.execute(
                        "UPDATE update_status SET driver_id = $1, date_update_latest = NOW() "
                        "WHERE vehicle_id = $2 AND device_id = $3",
                        request.driver_id, vehicle_id, new_device_id
                    )
                    return {
                        "status": "driver_updated",
                        "vehicle_id": vehicle_id,
                        "device_id": new_device_id,
                        "driver_id": request.driver_id,
                        "previous_driver_id": actual_old_driver,
                        "migrated_trip_logs": 0,
                        "message": f"อัปเดตคนขับรถ {vehicle_id} จาก {actual_old_driver} → {request.driver_id} สำเร็จ"
                    }

                # ── 3. ถ้าบอร์ดใหม่ผูกกับรถอื่นอยู่ → ปลดออกก่อน ───────
                await conn.execute(
                    "UPDATE devices SET vehicle_id = NULL, active = false "
                    "WHERE id = $1 AND vehicle_id != $2",
                    new_device_id, vehicle_id
                )
                await conn.execute(
                    "DELETE FROM update_status WHERE device_id = $1 AND vehicle_id != $2",
                    new_device_id, vehicle_id
                )

                migrated_trips = 0

                if actual_old_device:
                    # ── 4a. Migrate trip_logs: อัปเดต vehicle_id ให้ถูก ──
                    migrate_result = await conn.execute(
                        """
                        UPDATE trip_logs
                        SET vehicle_id = $1
                        WHERE device_id = $2
                          AND (vehicle_id IS NULL OR vehicle_id = 0 OR vehicle_id != $1)
                        """,
                        vehicle_id, actual_old_device
                    )
                    try:
                        migrated_trips = int(migrate_result.split()[-1])
                    except Exception:
                        migrated_trips = 0

                    # ── 4b. ปลดบอร์ดเก่าออก ─────────────────────────────
                    await conn.execute(
                        "UPDATE devices SET vehicle_id = NULL, active = false WHERE id = $1",
                        actual_old_device
                    )
                    await conn.execute(
                        "DELETE FROM update_status WHERE vehicle_id = $1 AND device_id = $2",
                        vehicle_id, actual_old_device
                    )

                # ── 5. ผูกบอร์ดใหม่ ──────────────────────────────────────
                await conn.execute(
                    """
                    INSERT INTO devices (id, vehicle_id, active, driver_id)
                    VALUES ($1, $2, true, $3)
                    ON CONFLICT (id) DO UPDATE
                        SET vehicle_id = $2,
                            active     = true,
                            driver_id  = $3
                    """,
                    new_device_id, vehicle_id, request.driver_id
                )
                await conn.execute(
                    """
                    INSERT INTO update_status (vehicle_id, device_id, driver_id, date_update_latest)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (vehicle_id, device_id)
                    DO UPDATE SET driver_id = $3, date_update_latest = NOW()
                    """,
                    vehicle_id, new_device_id, request.driver_id
                )

                status = "registered" if not actual_old_device else "migrated"
                msg = (
                    f"ผูกบอร์ด {new_device_id} กับรถ {vehicle_id} สำเร็จ"
                    if not actual_old_device
                    else (
                        f"เปลี่ยนบอร์ด {actual_old_device} → {new_device_id} "
                        f"สำหรับรถ {vehicle_id} สำเร็จ"
                        + (f" (migrate trip_logs {migrated_trips} รายการ)" if migrated_trips > 0 else "")
                    )
                )

                return {
                    "status": status,
                    "vehicle_id": vehicle_id,
                    "device_id": new_device_id,
                    "previous_device_id": actual_old_device,
                    "migrated_trip_logs": migrated_trips,
                    "message": msg
                }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────
# GET Scoring Config — Current active config
# ─────────────────────────────────────────────────────────────

@router.get("/config/scoring/current")
async def get_current_scoring_config(
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX #3]
):
    """
    Get currently active scoring configuration

    **Authentication:** ต้องใส่ APIKEY header (FDD §13)

    Returns:
        Scoring config with all weights and thresholds
    """

    try:
        config = await pool.fetchrow(
            """
            SELECT 
                id, config_name, score_base, harsh_brake_deduct, harsh_accel_deduct,
                harsh_corner_deduct, speeding_deduct, idling_deduct, bump_deduct,
                harsh_brake_g, harsh_accel_g, harsh_corner_g, speeding_kmh_over,
                idle_min_threshold, max_deduct_per_trip, is_active, 
                effective_date, synced_from_odoo_at
            FROM scoring_config_cache
            WHERE is_active = true
            ORDER BY effective_date DESC
            LIMIT 1
            """
        )

        if not config:
            raise HTTPException(status_code=404, detail="No active config found")

        return {k: round(v, 4) if isinstance(v, float) else v
                for k, v in dict(config).items()}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────
# POST Scoring Config — Odoo push config ใหม่
# ─────────────────────────────────────────────────────────────

@router.post("/config/scoring", status_code=201)
async def push_scoring_config(
    request: ScoringConfigRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: str = Security(_verify_api_key),  # [FIX #3]
):
    """
    Odoo push scoring config ใหม่เข้า cache

    **Authentication:** ต้องใส่ APIKEY header (FDD §13) — endpoint นี้กระทบ
    การคำนวณโบนัสพนักงานโดยตรง (FDD §12.4) จึงต้องป้องกันไม่ให้ใครก็ได้
    push config ปลอมเข้ามา

    - Deactivate config เก่าทั้งหมดก่อน
    - Insert config ใหม่ พร้อม is_active = true
    - คืน config ที่เพิ่งบันทึก

    Body: ScoringConfigRequest (config_name บังคับ ที่เหลือมี default)
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():

                # 1. Deactivate ทุก config ที่ active อยู่
                await conn.execute(
                    "UPDATE scoring_config_cache SET is_active = false WHERE is_active = true"
                )

                # 2. Insert config ใหม่
                row = await conn.fetchrow(
                    """
                    INSERT INTO scoring_config_cache (
                        config_name,
                        score_base,
                        harsh_brake_deduct,
                        harsh_accel_deduct,
                        harsh_corner_deduct,
                        speeding_deduct,
                        idling_deduct,
                        bump_deduct,
                        harsh_brake_g,
                        harsh_accel_g,
                        harsh_corner_g,
                        speeding_kmh_over,
                        idle_min_threshold,
                        max_deduct_per_trip,
                        is_active,
                        effective_date,
                        synced_from_odoo_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13, $14,
                        true,
                        CURRENT_DATE,
                        $15
                    )
                    RETURNING
                        id, config_name, score_base,
                        harsh_brake_deduct, harsh_accel_deduct,
                        harsh_corner_deduct, speeding_deduct,
                        idling_deduct, bump_deduct,
                        harsh_brake_g, harsh_accel_g, harsh_corner_g,
                        speeding_kmh_over, idle_min_threshold,
                        max_deduct_per_trip, is_active,
                        effective_date, synced_from_odoo_at
                    """,
                    request.config_name,
                    request.score_base,
                    request.harsh_brake_deduct,
                    request.harsh_accel_deduct,
                    request.harsh_corner_deduct,
                    request.speeding_deduct,
                    request.idling_deduct,
                    request.bump_deduct,
                    request.harsh_brake_g,
                    request.harsh_accel_g,
                    request.harsh_corner_g,
                    request.speeding_kmh_over,
                    request.idle_min_threshold,
                    request.max_deduct_per_trip,
                    request.synced_from_odoo_at,
                )

                return {
                    "status": "success",
                    "message": f"Config '{request.config_name}' activated",
                    "config": {
                        k: round(v, 4) if isinstance(v, float) else v
                        for k, v in dict(row).items()
                    }
                }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))