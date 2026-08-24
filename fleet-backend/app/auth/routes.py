from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional, Any
import asyncpg

from app.auth.models import (
    get_user_by_username,
    update_last_login,
    create_api_key,
    list_api_keys,
    revoke_api_key,
)
from app.auth.dependencies import (
    verify_password,
    hash_password,
    create_access_token,
    get_current_user_jwt,
    require_admin,
)
from app.database import get_db_pool

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    role: str = "viewer"


class CreateApiKeyRequest(BaseModel):
    name: str


class ApiKeyResponse(BaseModel):
    id: int
    name: str
    api_key: str
    created_at: Any


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="เข้าสู่ระบบ"
)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    try:

        async with pool.acquire() as conn:

            user = await get_user_by_username(
                conn,
                form.username
            )

            if not user:
                raise HTTPException(
                    status_code=401,
                    detail="Username หรือ Password ไม่ถูกต้อง"
                )

            if not verify_password(
                form.password,
                user["hashed_password"]
            ):
                raise HTTPException(
                    status_code=401,
                    detail="Username หรือ Password ไม่ถูกต้อง"
                )

            await update_last_login(
                conn,
                user["id"]
            )

            token = create_access_token(
                user_id=user["id"],
                username=user["username"],
                role=user["role"]
            )

            return LoginResponse(
                access_token=token,
                username=user["username"],
                role=user["role"]
            )

    except HTTPException:
        raise

    except Exception as e:
        print("LOGIN ERROR:", e)
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@router.get(
    "/me",
    summary="ดูข้อมูลผู้ใช้ปัจจุบัน"
)
async def get_me(
    current_user: dict = Depends(
        get_current_user_jwt
    )
):
    return current_user


@router.post(
    "/register",
    summary="สร้างผู้ใช้ใหม่ (Admin)"
)
async def register_user(
    body: RegisterRequest,
    current_user: dict = Depends(
        require_admin
    ),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    async with pool.acquire() as conn:

        existing = await get_user_by_username(
            conn,
            body.username
        )

        if existing:
            raise HTTPException(
                status_code=400,
                detail="Username นี้มีอยู่แล้ว"
            )

        hashed = hash_password(
            body.password
        )

        row = await conn.fetchrow(
            """
            INSERT INTO users
            (
                username,
                hashed_password,
                full_name,
                role,
                is_active
            )
            VALUES
            (
                $1,
                $2,
                $3,
                $4,
                TRUE
            )
            RETURNING
                id,
                username,
                full_name,
                role,
                created_at
            """,
            body.username,
            hashed,
            body.full_name,
            body.role
        )

        return {
            "message": "สร้าง user สำเร็จ",
            "user": dict(row)
        }


@router.post(
    "/api-keys",
    response_model=ApiKeyResponse,
    summary="สร้าง API Key ใหม่ (Admin เท่านั้น)",
)
async def create_new_api_key(
    body: CreateApiKeyRequest,
    current_user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    async with pool.acquire() as conn:
        row = await create_api_key(conn, body.name)

    return ApiKeyResponse(
        id=row["id"],
        name=row["name"],
        api_key=row["key_hash"],
        created_at=row["created_at"],
    )


@router.get(
    "/api-keys",
    summary="ดูรายการ API Key ทั้งหมด (Admin)",
)
async def list_all_api_keys(
    current_user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    async with pool.acquire() as conn:
        keys = await list_api_keys(conn)
    return {"total": len(keys), "keys": keys}


@router.delete(
    "/api-keys/{key_id}",
    summary="เพิกถอน API Key (Admin)",
)
async def revoke_existing_api_key(
    key_id: int,
    current_user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    async with pool.acquire() as conn:
        ok = await revoke_api_key(conn, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="ไม่พบ API Key นี้")
    return {"status": "revoked", "key_id": key_id}
