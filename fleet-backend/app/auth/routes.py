from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional, Any
import asyncpg

from app.auth.models import (
    get_user_by_username,
    get_user_by_username_any_status,
    create_pending_user,
    list_pending_users,
    approve_user,
    list_all_users_with_key_status,
    delete_user,
    admin_revoke_key_for_user,
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
    """ใช้โดย /auth/register (admin-created user, active ทันที)"""
    username: str
    password: str
    full_name: Optional[str] = None
    role: str = "viewer"


class SignupRequest(BaseModel):
    """ใช้โดย /auth/signup (self-signup สาธารณะ, pending approval)"""
    username: str
    email: str
    password: str
    full_name: Optional[str] = None


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
                    detail="Username หรือ Password ไม่ถูกต้อง หรือบัญชียังไม่ได้รับการอนุมัติ"
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


@router.post(
    "/signup",
    status_code=201,
    summary="สมัครสมาชิกด้วยตัวเอง (สถานะ pending — รอ admin อนุมัติ)",
)
async def signup(
    body: SignupRequest,
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    try:
        async with pool.acquire() as conn:

            existing = await get_user_by_username_any_status(conn, body.username)
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail="Username หรือ Email นี้มีอยู่แล้วในระบบ",
                )

            existing_email = await get_user_by_username_any_status(conn, body.email)
            if existing_email:
                raise HTTPException(
                    status_code=400,
                    detail="Username หรือ Email นี้มีอยู่แล้วในระบบ",
                )

            hashed = hash_password(body.password)
            user = await create_pending_user(
                conn,
                username=body.username,
                email=body.email,
                hashed_password=hashed,
                full_name=body.full_name,
            )

        return {
            "message": "สมัครสมาชิกสำเร็จ — บัญชีของคุณรอการอนุมัติจากผู้ดูแลระบบ",
            "user": user,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    summary="สร้างผู้ใช้ใหม่ (Admin สร้างโดยตรง — active ทันที ไม่ผ่าน pending)"
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


# =====================================================
# Admin — approval queue / user management
# =====================================================

@router.get(
    "/admin/pending-users",
    summary="[Admin] รายชื่อผู้สมัครที่รออนุมัติ",
)
async def get_pending_users(
    current_user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    async with pool.acquire() as conn:
        pending = await list_pending_users(conn)
    return {"total": len(pending), "pending_users": pending}


@router.post(
    "/admin/users/{user_id}/approve",
    summary="[Admin] อนุมัติให้ user เข้าใช้งาน (login ได้)",
)
async def approve_pending_user(
    user_id: int,
    current_user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    async with pool.acquire() as conn:
        approved = await approve_user(conn, user_id)

    if not approved:
        raise HTTPException(
            status_code=404,
            detail="ไม่พบ user นี้ หรือ user ถูกอนุมัติไปแล้ว",
        )

    return {"status": "approved", "user": approved}


@router.get(
    "/admin/users",
    summary="[Admin] รายชื่อ user ทั้งหมด พร้อมสถานะ API key",
)
async def get_all_users(
    current_user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    async with pool.acquire() as conn:
        users = await list_all_users_with_key_status(conn)
    return {"total": len(users), "users": users}


@router.post(
    "/admin/users/{user_id}/revoke-key",
    summary="[Admin] ระงับการใช้งาน API key ของ user คนนี้ (account ยังอยู่)",
)
async def admin_revoke_user_key(
    user_id: int,
    current_user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    async with pool.acquire() as conn:
        revoked = await admin_revoke_key_for_user(conn, user_id)

    if not revoked:
        raise HTTPException(
            status_code=404,
            detail="ไม่พบ API key ที่กำลังใช้งานอยู่สำหรับ user นี้",
        )

    return {"status": "key_revoked", "user_id": user_id}


@router.delete(
    "/admin/users/{user_id}",
    summary="[Admin] ลบ user ทิ้งถาวร (key ที่ผูกอยู่ถูกลบตามไปด้วย) "
            "— user ต้องสมัครใหม่เพื่อรับ key ใหม่",
)
async def admin_delete_user(
    user_id: int,
    current_user: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_db_pool),
):
    if current_user["user_id"] == user_id:
        raise HTTPException(
            status_code=400,
            detail="ไม่สามารถลบบัญชีของตัวเองได้",
        )

    async with pool.acquire() as conn:
        deleted = await delete_user(conn, user_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="ไม่พบ user นี้")

    return {"status": "deleted", "user_id": user_id}


# =====================================================
# Admin — generic API key management (unrelated to per-user keys,
# kept from original for service/integration keys, e.g. Odoo)
# =====================================================

@router.post(
    "/api-keys",
    response_model=ApiKeyResponse,
    summary="สร้าง API Key ใหม่ (Admin เท่านั้น) — สำหรับ service/integration key",
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