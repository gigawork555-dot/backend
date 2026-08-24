import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, Header, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.config import settings
from app.auth.models import get_api_key, get_user_by_id, update_key_last_used
from app.cache import rate_limit_check

JWT_SECRET      = "fleet_jwt_secret_change_this_in_production"
JWT_ALGORITHM   = "HS256"
JWT_EXPIRE_MIN  = 60 * 8

api_key_header = APIKeyHeader(name="APIKEY", auto_error=False)

# [แก้ไข] เดิมใช้ OAuth2PasswordBearer(tokenUrl="/auth/login") เป็น
# dependency ของ get_current_user_jwt() — คลาสนี้สืบทอดจาก SecurityBase
# ทำให้ FastAPI auto-register เป็น security scheme ("OAuth2, password")
# แล้วโผล่ปุ่ม Authorize ใน Swagger /docs โดยอัตโนมัติ
#
# ทีมไม่ได้ใช้ flow login ผ่าน Swagger แล้ว (ย้ายไปใช้ portal.html ที่ยิง
# /auth/login เอง แล้วแนบ JWT ใน header ของทุก request ต่อจากนั้น) จึง
# ตัดปุ่มนี้ออกโดยเปลี่ยนมาอ่าน Authorization header ตรงๆ ด้วย
# fastapi.Header (ธรรมดา ไม่ใช่ SecurityBase) — ไม่ถูก FastAPI ทำเป็น
# security scheme จึงไม่มีปุ่ม Authorize (OAuth2, password) ใน Swagger
# อีกต่อไป แต่ endpoint ที่ต้อง login ยังคงตรวจ JWT จาก header
# "Authorization: Bearer <token>" ได้ปกติทุกจุดเหมือนเดิม
RATE_LIMIT_DEFAULT_LIMIT: int = 60
RATE_LIMIT_DEFAULT_WINDOW_SECONDS: int = 60


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


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


def _extract_bearer_token(
    authorization: Optional[str] = Header(default=None)
) -> Optional[str]:
    """
    อ่าน JWT จาก header "Authorization: Bearer <token>" ตรงๆ ด้วย
    fastapi.Header (ไม่ใช่ SecurityBase subclass เหมือน
    OAuth2PasswordBearer เดิม) จึงไม่ถูก FastAPI เอาไปสร้างเป็น
    security scheme ใน OpenAPI schema — ผลคือไม่มีปุ่ม Authorize
    (OAuth2, password) โผล่ในหน้า Swagger /docs อีกต่อไป
    """
    if not authorization:
        return None

    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]

    return None


async def get_current_user_jwt(token: Optional[str] = Depends(_extract_bearer_token)):
    if not token:
        raise HTTPException(status_code=401, detail="กรุณา Login ก่อน")
    payload = decode_access_token(token)
    return {
        "user_id":  int(payload["sub"]),
        "username": payload["username"],
        "role":     payload["role"],
    }


async def require_admin(current_user: dict = Depends(get_current_user_jwt)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="ต้องการสิทธิ์ Admin")
    return current_user


async def verify_api_key(
    request: Request,
    api_key: str = Security(api_key_header),
):
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="ต้องใส่ API Key ใน Header: APIKEY"
        )

    from app.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        key_data = await get_api_key(conn, api_key)
        if not key_data:
            raise HTTPException(status_code=401, detail="API Key ไม่ถูกต้องหรือหมดอายุ")

        client_ip = request.client.host if request.client else "unknown"
        await update_key_last_used(conn, api_key, client_ip)

    return key_data


async def verify_odoo_api_key(api_key: str = Security(api_key_header)):
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


def _rate_limit_identity(request: Request) -> str:
    api_key = request.headers.get("APIKEY") or request.headers.get("X-API-Key")
    if api_key:
        return f"apikey:{api_key}"

    client_ip = request.client.host if request.client else "unknown"
    return f"ip:{client_ip}"


async def rate_limit_guard(
    request: Request,
    limit: int = RATE_LIMIT_DEFAULT_LIMIT,
    window_seconds: int = RATE_LIMIT_DEFAULT_WINDOW_SECONDS,
) -> None:
    identity = _rate_limit_identity(request)

    allowed = await rate_limit_check(
        key=f"{request.url.path}:{identity}",
        limit=limit,
        window_seconds=window_seconds,
    )

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded — สูงสุด {limit} requests "
                f"ต่อ {window_seconds} วินาที กรุณาลองใหม่ภายหลัง"
            ),
        )


def make_rate_limit_guard(
    limit: int = RATE_LIMIT_DEFAULT_LIMIT,
    window_seconds: int = RATE_LIMIT_DEFAULT_WINDOW_SECONDS,
):
    async def _guard(request: Request) -> None:
        await rate_limit_guard(request, limit=limit, window_seconds=window_seconds)

    return _guard
