import re
from fastapi import APIRouter, HTTPException, Depends, Security
from pydantic import BaseModel, field_validator
import asyncpg
from typing import List, Optional
from datetime import datetime

from app.database import get_db_pool
from app.auth.dependencies import verify_api_key

router = APIRouter(prefix="/api/v1", tags=["Config"])

DEVICE_ID_PATTERN = re.compile(r"^KTC-\d{3}$")


def _validate_device_id_format(v: str, field_name: str = "device_id") -> str:
    if v is None:
        return v

    cleaned = v.strip().upper()

    if not DEVICE_ID_PATTERN.match(cleaned):
        raise ValueError(
            f"{field_name} ต้องเป็นรูปแบบ KTC-XXX เท่านั้น "
            f"(เช่น KTC-001, KTC-002) — ได้รับค่า: '{v}'"
        )

    return cleaned


class RegisterDeviceRequest(BaseModel):
    device_id: str
    device_name: str
    vehicle_id: int

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, v: str) -> str:
        return _validate_device_id_format(v, "device_id")


class RegisterDeviceBatchRequest(BaseModel):
    devices: List[RegisterDeviceRequest]


class VehicleConfigUpdate(BaseModel):
    vehicle_id: int
    new_device_id: str
    old_device_id: Optional[str] = None
    driver_id: Optional[int] = None

    @field_validator("new_device_id")
    @classmethod
    def validate_new_device_id(cls, v: str) -> str:
        return _validate_device_id_format(v, "new_device_id")

    @field_validator("old_device_id")
    @classmethod
    def validate_old_device_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        return _validate_device_id_format(v, "old_device_id")


class ScoringConfigRequest(BaseModel):
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


async def _register_single(
    conn: asyncpg.Connection,
    item: RegisterDeviceRequest
) -> dict:

    device_id = item.device_id.strip().upper()
    vehicle_id = item.vehicle_id

    existing_same_binding = await conn.fetchrow(
        """
        SELECT vehicle_id FROM update_status 
        WHERE device_id = $1 AND vehicle_id = $2
        """,
        device_id, vehicle_id
    )

    if existing_same_binding:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Device {device_id} is already bound to vehicle {vehicle_id}. "
                f"No changes made."
            )
        )

    existing_other_binding = await conn.fetchrow(
        """
        SELECT vehicle_id FROM update_status 
        WHERE device_id = $1 AND vehicle_id != $2
        """,
        device_id, vehicle_id
    )

    if existing_other_binding:
        other_vehicle_id = existing_other_binding['vehicle_id']
        raise HTTPException(
            status_code=409,
            detail=(
                f"Device {device_id} is already bound to vehicle {other_vehicle_id}. "
                f"Use PUT /config/vehicle to migrate."
            )
        )

    existing_vehicle_device = await conn.fetchrow(
        """
        SELECT device_id FROM update_status 
        WHERE vehicle_id = $1 AND device_id != $2
        """,
        vehicle_id, device_id
    )

    if existing_vehicle_device:
        other_device_id = existing_vehicle_device['device_id']
        raise HTTPException(
            status_code=409,
            detail=(
                f"Vehicle {vehicle_id} is already bound to device {other_device_id}. "
                f"Cannot bind to {device_id}. Use PUT /config/vehicle to replace."
            )
        )

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


@router.get("/devices")
async def get_devices(
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
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


@router.get("/config_device")
async def get_device_config(
    device_id: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
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


@router.post("/config_device/register", status_code=201)
async def register_device_single(
    request: RegisterDeviceRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    try:
        async with pool.acquire() as conn:
            register_result = await _register_single(conn, request)
            return register_result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config_device/register/batch", status_code=201)
async def register_device_batch(
    request: RegisterDeviceBatchRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    if not request.devices:
        raise HTTPException(status_code=400, detail="No devices provided")

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():

                results = []

                for item in request.devices:
                    try:
                        batch_item_result = await _register_single(conn, item)
                        results.append(batch_item_result)

                    except HTTPException as e:
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


@router.put("/config/vehicle")
async def update_vehicle_config(
    request: VehicleConfigUpdate,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():

                vehicle_id    = request.vehicle_id
                new_device_id = request.new_device_id
                old_device_id = request.old_device_id

                current = await conn.fetchrow(
                    "SELECT device_id, driver_id FROM update_status WHERE vehicle_id = $1 LIMIT 1",
                    vehicle_id
                )
                actual_old_device = current["device_id"] if current else None
                actual_old_driver = current["driver_id"] if current else None

                if old_device_id and actual_old_device and old_device_id != actual_old_device:
                    pass

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

                    await conn.execute(
                        "UPDATE devices SET vehicle_id = NULL, active = false WHERE id = $1",
                        actual_old_device
                    )
                    await conn.execute(
                        "DELETE FROM update_status WHERE vehicle_id = $1 AND device_id = $2",
                        vehicle_id, actual_old_device
                    )

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


@router.get("/config/scoring/current")
async def get_current_scoring_config(
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
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


@router.post("/config/scoring", status_code=201)
async def push_scoring_config(
    request: ScoringConfigRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
    api_key: dict = Security(verify_api_key),
):
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():

                await conn.execute(
                    "UPDATE scoring_config_cache SET is_active = false WHERE is_active = true"
                )

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
