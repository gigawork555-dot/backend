# app/developer/routes.py
#
# "Generate once" self-service API key — FDD-adjacent internal policy
# (not in FDD v1.4 itself; a developer-portal UX decision).
#
# Rules:
#   - A user may GENERATE their key exactly once. No self-service
#     regenerate, no self-service revoke.
#   - If the key needs to be invalidated, an ADMIN revokes it
#     (account stays) or DELETES the user (account + key both gone,
#     forcing a fresh signup+approval to get a new key).
#   - user_id is ALWAYS taken from the JWT claim — never from the
#     request body or query string.

from fastapi import APIRouter, Depends, HTTPException
import asyncpg

from app.database import get_db_pool
from app.auth.dependencies import get_current_user_jwt
from app.auth.models import (
    get_api_key_for_user,
    create_api_key,
)

router = APIRouter(
    prefix="/developer",
    tags=["Developer Portal"],
    include_in_schema=False,
)


def _mask_key_prefix(key_prefix: str | None) -> str:
    if not key_prefix:
        return "****"
    return f"{key_prefix}****"


@router.get("/me/api-key", summary="ดูสถานะ API Key ของตัวเอง (masked)")
async def get_my_api_key(
    current_user: dict = Depends(get_current_user_jwt),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    user_id = current_user["user_id"]

    async with pool.acquire() as conn:
        key_row = await get_api_key_for_user(conn, user_id)

    if not key_row:
        return {"has_key": False, "can_generate": True}

    return {
        "has_key": True,
        "can_generate": False,  # generate-once: ถ้ามี key แล้ว generate ซ้ำไม่ได้
        "masked_key": _mask_key_prefix(key_row.get("key_prefix")),
        "created_at": key_row.get("created_at"),
        "last_used": key_row.get("last_used"),
    }


@router.post(
    "/me/api-key/generate",
    summary="สร้าง API Key — ทำได้ครั้งเดียวต่อ 1 user เท่านั้น",
)
async def generate_my_api_key_once(
    current_user: dict = Depends(get_current_user_jwt),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    user_id = current_user["user_id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await get_api_key_for_user(conn, user_id)
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "คุณมี API Key อยู่แล้ว — ระบบนี้สร้างได้ครั้งเดียวต่อ 1 user "
                        "หาก key หายหรือถูกระงับ กรุณาติดต่อผู้ดูแลระบบ"
                    ),
                )

            new_key = await create_api_key(conn, "primary-key", user_id=user_id)

    return {
        "status": "success",
        "message": "สร้าง API Key สำเร็จ — คัดลอกไว้ตอนนี้ ระบบจะไม่แสดงค่านี้ซ้ำอีก",
        "api_key": new_key["api_key"],  # plaintext, shown once only
        "masked_key": _mask_key_prefix(new_key.get("key_prefix")),
        "created_at": new_key.get("created_at"),
    }