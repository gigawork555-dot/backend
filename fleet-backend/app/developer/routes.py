# app/developer/routes.py
#
# "1 user = 1 API Key" self-service endpoints.
# FDD v1.4 §13 — Authentication: JWT token สำหรับ API.
# Single-tenant (Kotchasaan internal) — NO company / multi-tenant concept.
#
# All endpoints require a valid JWT (Depends(get_current_user_jwt)) first.
# user_id is ALWAYS taken from the JWT claim (current_user["user_id"]) —
# never accepted from the request body or query string.
#
# [แก้ไข #1] ตัด step-up auth (re-submit password) ออกทั้งหมด — endpoint
# ยืนยันตัวตนด้วย JWT (จาก /auth/login ผ่าน portal.html) เพียงอย่างเดียว
#
# [แก้ไข #2] ทั้ง router นี้ตั้ง include_in_schema=False — endpoint ยัง
# ทำงานได้ปกติทุกอย่าง (portal.html ยิงตรงมาที่ /developer/... ได้เหมือน
# เดิม) แค่ "ไม่โผล่" ในหน้า Swagger /docs อีกต่อไป เพราะทีมใช้งานผ่าน
# portal เป็นหลักแล้ว ไม่มีใครต้องทดสอบ endpoint กลุ่มนี้ผ่าน Swagger

from fastapi import APIRouter, Depends, HTTPException
import asyncpg

from app.database import get_db_pool
from app.auth.dependencies import get_current_user_jwt
from app.auth.models import (
    get_api_key_for_user,
    regenerate_api_key_for_user,
    revoke_user_api_key,
)

router = APIRouter(
    prefix="/developer",
    tags=["Developer Portal"],
    include_in_schema=False,  # [แก้ไข #2] ซ่อนทั้งกลุ่มจากหน้า Swagger /docs
)


def _mask_key_prefix(key_prefix: str | None) -> str:
    """
    Build a masked display string from the stored (non-secret) prefix,
    e.g. 'ktc_ab12cdef' -> 'ktc_ab12****'
    Never touches the actual secret — key_prefix itself is already just
    a short, non-sensitive identifier stored for display purposes.
    """
    if not key_prefix:
        return "****"
    return f"{key_prefix}****"


@router.get("/me/api-key", summary="ดู API Key ของตัวเอง (masked)")
async def get_my_api_key(
    current_user: dict = Depends(get_current_user_jwt),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    user_id = current_user["user_id"]

    async with pool.acquire() as conn:
        key_row = await get_api_key_for_user(conn, user_id)

    if not key_row:
        return {"has_key": False}

    return {
        "has_key": True,
        "masked_key": _mask_key_prefix(key_row.get("key_prefix")),
        "created_at": key_row.get("created_at"),
        "last_rotated_at": key_row.get("last_rotated_at"),
        "last_used": key_row.get("last_used"),
    }


@router.post("/me/api-key/regenerate", summary="สร้าง API Key ใหม่")
async def regenerate_my_api_key(
    current_user: dict = Depends(get_current_user_jwt),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    user_id = current_user["user_id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            new_key = await regenerate_api_key_for_user(conn, user_id)

    return {
        "status": "success",
        "message": "สร้าง API Key ใหม่สำเร็จ — คัดลอกไว้ตอนนี้ จะไม่แสดงซ้ำอีก",
        "api_key": new_key["api_key"],  # plaintext, shown once only
        "masked_key": _mask_key_prefix(new_key.get("key_prefix")),
        "created_at": new_key.get("created_at"),
    }


@router.post("/me/api-key/revoke", summary="ปิดใช้งาน API Key")
async def revoke_my_api_key(
    current_user: dict = Depends(get_current_user_jwt),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    user_id = current_user["user_id"]

    async with pool.acquire() as conn:
        revoked = await revoke_user_api_key(conn, user_id)

    if not revoked:
        raise HTTPException(status_code=404, detail="ไม่พบ API Key ที่เปิดใช้งานอยู่")

    return {"status": "revoked"}
