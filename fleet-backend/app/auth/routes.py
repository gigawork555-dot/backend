from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from typing import Optional

from app.auth.models import (
    get_user_by_username,
    update_last_login,
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


# ==================================================
# RESPONSE MODELS
# ==================================================

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


# ==================================================
# LOGIN
# ==================================================

@router.post(
    "/login",
    response_model=LoginResponse,
    summary="เข้าสู่ระบบ"
)
async def login(
    form: OAuth2PasswordRequestForm = Depends()
):
    try:

        pool = await get_db_pool()

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


# ==================================================
# CURRENT USER
# ==================================================

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


# ==================================================
# REGISTER USER
# ==================================================

@router.post(
    "/register",
    summary="สร้างผู้ใช้ใหม่ (Admin)"
)
async def register_user(
    body: RegisterRequest,
    current_user: dict = Depends(
        require_admin
    )
):
    pool = await get_db_pool()

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