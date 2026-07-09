# app/cache.py

"""
Redis Cache Layer

FDD v1.4 §11.1 Services Architecture:
    Cache | Redis 7 | 6379 | Session, rate limit, real-time dashboard

Responsibilities:
- สร้าง/ใช้ redis.asyncio.Redis connection ร่วมกันทั้งแอป
  (pattern เดียวกับ asyncpg pool ใน app/database.py)
- Session helpers — เสริม JWT เท่านั้น (JWT ยังเป็น stateless source
  of truth ของ auth เหมือนเดิม ไม่เปลี่ยน flow เดิม)
- Rate limit แบบ atomic (INCR + EXPIRE ผ่าน pipeline กัน race condition)
- Fleet-live SSE snapshot cache (TTL สั้น) กันหลาย client ยิง query
  telemetry_raw ซ้ำพร้อมกันทุก 5 วินาที

หลักการสำคัญ (ตามสเป็ค): Redis ต้องไม่เป็น single point of failure
ทุกฟังก์ชัน public ในไฟล์นี้ครอบ try/except ของตัวเอง และ degrade
gracefully เสมอ:
    * session helpers  -> คืน None / no-op เมื่อ Redis ใช้งานไม่ได้
    * rate_limit_check -> fail-OPEN (อนุญาตให้ผ่าน) เมื่อ Redis ล่ม
    * fleet-live cache -> ผู้เรียกต้อง fallback ไป query DB ตรงเอง
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Shared pool (pattern เดียวกับ app/database.py)
# ──────────────────────────────────────────────────────────────

_redis_pool: Optional[redis.Redis] = None
_pool_lock = asyncio.Lock()

# TTLs
SESSION_TTL_SECONDS: int = 60 * 60 * 8       # 8 ชั่วโมง — ตรงกับ JWT_EXPIRE_MIN
FLEET_LIVE_TTL_SECONDS: int = 3              # 2-3 วินาที ตามสเป็ค


async def create_redis_pool() -> Optional[redis.Redis]:
    """
    สร้างและเริ่มต้น redis.asyncio.Redis client ที่ใช้ร่วมกันทั้งแอป

    คืน None แทนที่จะ raise ถ้า Redis ต่อไม่ได้ตอน startup — Redis
    เป็น cache/optional layer ไม่ใช่ hard dependency แอปต้องยัง boot
    ได้ตามปกติแม้ Redis จะล่มอยู่ (ต่างจาก create_db_pool() ที่ DB
    เป็น hard dependency จริง)
    """
    global _redis_pool

    if _redis_pool is not None:
        logger.info("Redis pool already exists")
        return _redis_pool

    async with _pool_lock:

        # Double-check หลัง acquire lock
        if _redis_pool is not None:
            return _redis_pool

        try:
            logger.info("Creating Redis pool...")

            client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )

            # ping เพื่อยืนยันว่าต่อได้จริง — ถ้าไม่ได้ ไม่ raise ต่อ
            # เพื่อไม่ให้ FastAPI lifespan ทั้งตัว fail เพราะ Redis ล่ม
            await client.ping()

            _redis_pool = client
            logger.info("Redis pool created successfully")
            return _redis_pool

        except Exception:
            logger.exception(
                "Redis connection failed — caching/rate-limit features "
                "จะ degrade gracefully (fail-open / fallback DB) ต่อไป"
            )
            _redis_pool = None
            return None


async def get_redis_pool() -> Optional[redis.Redis]:
    """คืน Redis pool ที่มีอยู่ หรือ None ถ้ายังไม่พร้อม/ต่อไม่ได้"""
    return _redis_pool


async def close_redis_pool() -> None:
    """ปิด Redis pool อย่างปลอดภัยตอน shutdown"""
    global _redis_pool

    if _redis_pool is None:
        logger.info("Redis pool already closed")
        return

    try:
        logger.info("Closing Redis pool...")
        await _redis_pool.aclose()
        logger.info("Redis pool closed successfully")

    except Exception:
        logger.exception("Error while closing Redis pool")

    finally:
        _redis_pool = None


# ──────────────────────────────────────────────────────────────
# 1) Session helpers — เสริม JWT เท่านั้น ไม่ใช่ source of truth
# ──────────────────────────────────────────────────────────────

def _session_key(token_id: str) -> str:
    return f"session:{token_id}"


async def cache_set_session(
    token_id: str,
    session_data: dict,
    ttl_seconds: int = SESSION_TTL_SECONDS,
) -> bool:
    """
    เก็บข้อมูล session เสริมใน Redis (เช่นสำหรับ revoke/track) —
    JWT ยังเป็น source of truth ของ auth เหมือนเดิม ฟังก์ชันนี้เป็น
    ส่วนเสริมเท่านั้น ไม่ raise แม้ Redis จะล่ม (คืน False แทน)
    """
    pool = await get_redis_pool()
    if pool is None:
        return False

    try:
        await pool.set(
            _session_key(token_id),
            json.dumps(session_data, default=str),
            ex=ttl_seconds,
        )
        return True
    except Exception:
        logger.warning(
            "cache_set_session ล้มเหลว — ทำงานต่อโดยไม่มี cache",
            exc_info=True,
        )
        return False


async def cache_get_session(token_id: str) -> Optional[dict]:
    """ดึงข้อมูล session จาก Redis — คืน None ถ้า miss หรือ Redis ล่ม"""
    pool = await get_redis_pool()
    if pool is None:
        return None

    try:
        raw = await pool.get(_session_key(token_id))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        logger.warning(
            "cache_get_session ล้มเหลว — ทำงานต่อโดยไม่มี cache",
            exc_info=True,
        )
        return None


async def cache_delete_session(token_id: str) -> bool:
    """ลบ/revoke session (เช่นตอน logout) — ล้มเหลวแบบเงียบ"""
    pool = await get_redis_pool()
    if pool is None:
        return False

    try:
        await pool.delete(_session_key(token_id))
        return True
    except Exception:
        logger.warning("cache_delete_session ล้มเหลว", exc_info=True)
        return False


# ──────────────────────────────────────────────────────────────
# 2) Rate limiting — atomic INCR + EXPIRE ผ่าน pipeline
# ──────────────────────────────────────────────────────────────

async def rate_limit_check(
    key: str,
    limit: int,
    window_seconds: int,
) -> bool:
    """
    ตรวจ rate limit แบบ fixed-window ด้วย INCR + EXPIRE

    คืน True ถ้า "อนุญาตให้ผ่าน", False ถ้าเกิน limit ในหน้าต่างเวลานั้น

    Fail-OPEN: ถ้า Redis ต่อไม่ได้ คืน True (อนุญาต) เสมอ — Redis ล่ม
    ต้องไม่ทำให้ API ทั้งระบบใช้งานไม่ได้ (ตามสเป็ค ห้าม fail-closed)

    Atomicity: ส่ง INCR + EXPIRE เป็น pipeline เดียว (transaction=True)
    เพื่อไม่ให้ request อื่นแทรกกลางระหว่าง increment กับตั้ง TTL —
    ถ้าไม่ atomic จะเกิดบั๊ก key ที่ไม่มี TTL เลย (ค้างถาวร / rate
    limit ล็อกผู้ใช้ไปตลอดกาลโดยไม่ reset)

    หมายเหตุ EXPIRE NX: ใช้ nx=True เพื่อไม่ให้ reset TTL ทับของเดิม
    ถ้า key มีอยู่แล้ว (ป้องกัน window เลื่อนไปเรื่อยๆ ไม่มีวันหมดอายุ)
    """
    pool = await get_redis_pool()
    if pool is None:
        # Redis ล่ม -> fail-open
        return True

    try:
        rate_key = f"ratelimit:{key}"

        async with pool.pipeline(transaction=True) as pipe:
            pipe.incr(rate_key)
            pipe.expire(rate_key, window_seconds, nx=True)
            results = await pipe.execute()

        current_count = results[0]
        return current_count <= limit

    except Exception:
        logger.warning(
            "rate_limit_check ล้มเหลว — fail-open (อนุญาตให้ request ผ่าน)",
            exc_info=True,
        )
        return True


# ──────────────────────────────────────────────────────────────
# 3) Fleet-live SSE snapshot cache
# ──────────────────────────────────────────────────────────────

_FLEET_LIVE_KEY = "fleet:live:snapshot"


async def cache_fleet_live_snapshot(
    data: list,
    ttl_seconds: int = FLEET_LIVE_TTL_SECONDS,
) -> bool:
    """
    Cache ผล query fleet-live ล่าสุดไว้ช่วงสั้นๆ เพื่อไม่ให้หลาย SSE
    client ยิง query telemetry_raw ซ้ำพร้อมกันทุก 5 วินาที
    """
    pool = await get_redis_pool()
    if pool is None:
        return False

    try:
        await pool.set(
            _FLEET_LIVE_KEY,
            json.dumps(data, default=str),
            ex=ttl_seconds,
        )
        return True
    except Exception:
        logger.warning("cache_fleet_live_snapshot ล้มเหลว", exc_info=True)
        return False


async def get_cached_fleet_live_snapshot() -> Optional[list]:
    """
    คืน fleet-live snapshot ที่ cache ไว้ หรือ None ถ้า miss/Redis ล่ม
    (ผู้เรียกต้อง fallback ไป query DB ตรงเองในกรณีนี้)
    """
    pool = await get_redis_pool()
    if pool is None:
        return None

    try:
        raw = await pool.get(_FLEET_LIVE_KEY)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        logger.warning("get_cached_fleet_live_snapshot ล้มเหลว", exc_info=True)
        return None
