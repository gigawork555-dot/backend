# app/auth/dependencies.py
# หน้าที่: ตรวจสอบ API Key และ JWT Token สำหรับ endpoint ทุกตัว
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer

from app.config import settings
from app.auth.models import get_api_key, get_user_by_id, update_key_last_used

# ── Secret Key สำหรับ JWT ─────────────────────────────────────
JWT_SECRET      = "fleet_jwt_secret_change_this_in_production"
JWT_ALGORITHM   = "HS256"
JWT_EXPIRE_MIN  = 60 * 8  # 8 ชั่วโมง

# ── Security Scheme ───────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
oauth2_scheme  = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# ────────────────────────────────────────────────────────────────
# Password Hashing
# ────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ────────────────────────────────────────────────────────────────
# JWT Token
# ────────────────────────────────────────────────────────────────

def create_access_token(user_id: int, username: str, role: str) -> str:
    payload = {
        "sub":      str(user_id),
        "username": username,
        "role":     role,
        "exp":      datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MIN),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token หมดอายุ กรุณา Login ใหม่")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token ไม่ถูกต้อง")


# ────────────────────────────────────────────────────────────────
# Dependencies — ใช้กับ endpoint ที่ต้องการ auth
# ────────────────────────────────────────────────────────────────

async def get_current_user_jwt(token: str = Depends(oauth2_scheme)):
    """ตรวจ JWT Token — ใช้กับ Dashboard / หน้าเว็บ"""
    if not token:
        raise HTTPException(status_code=401, detail="กรุณา Login ก่อน")
    payload = decode_access_token(token)
    return {
        "user_id":  int(payload["sub"]),
        "username": payload["username"],
        "role":     payload["role"],
    }


async def require_admin(current_user: dict = Depends(get_current_user_jwt)):
    """ต้องเป็น admin เท่านั้น"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="ต้องการสิทธิ์ Admin")
    return current_user


async def verify_api_key(
    request,
    api_key: str = Security(api_key_header),
):
    """ตรวจ API Key — ใช้กับ REST API ที่ให้ Odoo / ระบบภายนอกเรียก"""
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="ต้องใส่ API Key ใน Header: X-API-Key"
        )

    from app.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        key_data = await get_api_key(conn, api_key)
        if not key_data:
            raise HTTPException(status_code=401, detail="API Key ไม่ถูกต้องหรือหมดอายุ")

        # อัปเดต last_used
        client_ip = request.client.host if request.client else "unknown"
        await update_key_last_used(conn, api_key, client_ip)

    return key_data


async def verify_odoo_api_key(api_key: str = Security(api_key_header)):
    """ตรวจ API Key เฉพาะ scope=odoo — ใช้กับ /odoo/* endpoint"""
    if not api_key:
        raise HTTPException(status_code=401, detail="ต้องใส่ API Key")

    from app.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        key_data = await get_api_key(conn, api_key)
        if not key_data:
            raise HTTPException(status_code=401, detail="API Key ไม่ถูกต้อง")
        if key_data.get("scope", "general") not in ("odoo", "admin"):
            raise HTTPException(status_code=403, detail="API Key นี้ไม่มีสิทธิ์เรียก Odoo endpoint")

    return key_data
