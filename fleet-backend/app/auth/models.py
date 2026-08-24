# app/auth/models.py

import hashlib
import hmac
import secrets
import asyncpg
from typing import Optional


# =====================================================
# USERS
# =====================================================

async def get_user_by_username(
    conn: asyncpg.Connection,
    username: str
) -> Optional[dict]:

    row = await conn.fetchrow(
        """
        SELECT *
        FROM users
        WHERE username = $1
          AND is_active = TRUE
        """,
        username
    )

    return dict(row) if row else None


async def get_user_by_id(
    conn: asyncpg.Connection,
    user_id: int
) -> Optional[dict]:

    row = await conn.fetchrow(
        """
        SELECT
            id,
            username,
            full_name,
            role,
            is_active,
            created_at
        FROM users
        WHERE id = $1
        """,
        user_id
    )

    return dict(row) if row else None


async def update_last_login(
    conn: asyncpg.Connection,
    user_id: int
):
    """
    ตาราง users ของคุณไม่มี last_login
    จึงปล่อยผ่าน
    """
    return


# =====================================================
# API KEYS — 1 user = 1 active key (FDD §13)
#
# [SECURITY FIX] เดิม key_hash เก็บ secrets.token_hex(32) ตรงๆ ลง DB
# (คือ plaintext key เอง แค่ตั้งชื่อคอลัมน์ผิด — ไม่ได้ hash จริง)
# ถ้า DB รั่ว = ทุก API key รั่วทันที
#
# แก้ไข:
#   - generate_api_key() คืน plaintext key รูปแบบ "ktc_<32 bytes urlsafe>"
#     ให้ user เห็น "ครั้งเดียว" ตอนสร้าง/regenerate เท่านั้น
#   - เก็บเฉพาะ SHA-256 hash ของ key ลง DB (คอลัมน์ key_hash เดิม
#     ตอนนี้เก็บ hash จริงแล้ว) — DB รั่วไม่ทำให้ key ใช้งานได้
#   - verify (get_api_key) ต้อง hash ค่าที่รับจาก header ก่อนเทียบ
#     ด้วย timing-safe compare (hmac.compare_digest)
# =====================================================

API_KEY_PREFIX = "ktc_"


def _hash_api_key(plaintext_key: str) -> str:
    """SHA-256 hash ของ plaintext key — ใช้ทั้งตอนสร้างและตอน verify"""
    return hashlib.sha256(plaintext_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """
    สร้าง API key ใหม่

    Returns:
        (plaintext_key, key_hash, key_prefix)
        - plaintext_key: ค่าที่ต้องแสดงให้ user เห็น "ครั้งเดียว" เท่านั้น
        - key_hash:       SHA-256 hash สำหรับเก็บลง DB
        - key_prefix:     ส่วนหน้าของ key (สำหรับแสดง masked ภายหลัง เช่น
                          ktc_ab12...) ไม่ใช่ความลับ ใช้แค่ระบุ key ให้ user จำได้
    """
    random_part = secrets.token_urlsafe(32)
    plaintext_key = f"{API_KEY_PREFIX}{random_part}"
    key_hash = _hash_api_key(plaintext_key)
    # เก็บ prefix แบบสั้น ๆ ที่ไม่เปิดเผยความลับ (ktc_ + 8 ตัวแรกของ random part)
    key_prefix = plaintext_key[: len(API_KEY_PREFIX) + 8]
    return plaintext_key, key_hash, key_prefix


async def create_api_key(
    conn: asyncpg.Connection,
    key_name: str,
    user_id: Optional[int] = None,
) -> dict:
    """
    สร้าง API key ใหม่ในฐานข้อมูล

    หมายเหตุ backward-compat: ฟังก์ชันนี้ยังคง signature เดิม (key_name)
    ไว้สำหรับ caller เก่า (เช่น /auth/api-keys ของ admin) แต่เพิ่ม
    user_id เป็น optional parameter สำหรับ flow ใหม่ (1 user = 1 key)
    คืน dict ที่มี "api_key" (plaintext, แสดงครั้งเดียว) แนบมาด้วยเสมอ
    """

    plaintext_key, key_hash, key_prefix = generate_api_key()

    row = await conn.fetchrow(
        """
        INSERT INTO api_keys
        (
            name,
            key_hash,
            user_id,
            key_prefix,
            is_active,
            last_rotated_at
        )
        VALUES
        (
            $1,
            $2,
            $3,
            $4,
            TRUE,
            NOW()
        )
        RETURNING *
        """,
        key_name,
        key_hash,
        user_id,
        key_prefix,
    )

    result = dict(row)
    result["api_key"] = plaintext_key  # plaintext — แสดงครั้งเดียวเท่านั้น
    return result


async def get_api_key(
    conn: asyncpg.Connection,
    api_key: str
) -> Optional[dict]:
    """
    Verify a plaintext API key against the stored hash.

    [SECURITY FIX] ต้อง hash ค่าที่รับเข้ามาก่อนเทียบกับ DB เสมอ —
    ห้ามเทียบ plaintext กับ column ตรงๆ อีกต่อไป ใช้ timing-safe
    compare (hmac.compare_digest) เพื่อป้องกัน timing attack
    """
    if not api_key:
        return None

    candidate_hash = _hash_api_key(api_key)

    row = await conn.fetchrow(
        """
        SELECT *
        FROM api_keys
        WHERE is_active = TRUE
        """
    )

    # NOTE: ดึงเฉพาะแถวที่ active มาเทียบทีละแถวด้วย compare_digest
    # (ไม่ WHERE key_hash = $1 ตรงๆ เพื่อคง timing-safe semantics
    # แม้จำนวนแถว active จะน้อย — ระบบนี้ 1 user = 1 active key เท่านั้น
    # จึงจำนวนแถวที่ต้องวนเทียบเท่ากับจำนวน user ที่มี key เปิดใช้งานอยู่)
    rows = await conn.fetch(
        """
        SELECT *
        FROM api_keys
        WHERE is_active = TRUE
        """
    )

    for r in rows:
        stored_hash = r["key_hash"]
        if stored_hash and hmac.compare_digest(stored_hash, candidate_hash):
            return dict(r)

    return None


async def get_api_key_for_user(
    conn: asyncpg.Connection,
    user_id: int,
) -> Optional[dict]:
    """คืน active API key record (ไม่มี plaintext) ของ user คนนี้ ถ้ามี"""
    row = await conn.fetchrow(
        """
        SELECT *
        FROM api_keys
        WHERE user_id = $1
          AND is_active = TRUE
        """,
        user_id,
    )
    return dict(row) if row else None


async def revoke_user_api_key(
    conn: asyncpg.Connection,
    user_id: int,
) -> bool:
    """ปิดใช้งาน active key ของ user คนนี้ (ถ้ามี) — ไม่ออกใหม่"""
    result = await conn.execute(
        """
        UPDATE api_keys
        SET is_active = FALSE,
            revoked_at = NOW()
        WHERE user_id = $1
          AND is_active = TRUE
        """,
        user_id,
    )
    try:
        return int(result.split()[-1]) > 0
    except (ValueError, IndexError, AttributeError):
        return False


async def regenerate_api_key_for_user(
    conn: asyncpg.Connection,
    user_id: int,
    key_name: str = "developer-portal-key",
) -> dict:
    """
    Revoke key เก่า (ถ้ามี) แล้วออก key ใหม่ให้ user คนนี้ ภายใน
    transaction เดียวกัน (caller ต้องเปิด conn.transaction() ครอบ)

    Unique partial index (uq_api_keys_one_active_per_user) เป็นตัวกัน
    ชั้นสุดท้ายไม่ให้มี 2 key active พร้อมกันสำหรับ user เดียวกัน แม้จะ
    revoke ก่อนแล้วก็ตาม (belt-and-suspenders)
    """
    await revoke_user_api_key(conn, user_id)
    return await create_api_key(conn, key_name, user_id=user_id)


async def list_api_keys(
    conn: asyncpg.Connection
) -> list:

    rows = await conn.fetch(
        """
        SELECT
            id,
            name,
            user_id,
            key_prefix,
            is_active,
            created_at,
            last_rotated_at,
            revoked_at
        FROM api_keys
        ORDER BY created_at DESC
        """
    )

    return [dict(r) for r in rows]


async def revoke_api_key(
    conn: asyncpg.Connection,
    key_id: int
) -> bool:

    result = await conn.execute(
        """
        UPDATE api_keys
        SET is_active = FALSE,
            revoked_at = NOW()
        WHERE id = $1
        """,
        key_id
    )

    return result == "UPDATE 1"


async def update_key_last_used(
    conn: asyncpg.Connection,
    api_key: str,
    ip: str
):
    """
    อัปเดต last_used timestamp ใน api_keys

    [SECURITY FIX] ต้อง hash api_key ก่อน UPDATE WHERE เช่นกัน เพราะ
    key_hash column ตอนนี้เก็บ SHA-256 hash ไม่ใช่ plaintext แล้ว
    """
    candidate_hash = _hash_api_key(api_key)
    await conn.execute(
        """
        UPDATE api_keys
        SET last_used = NOW()
        WHERE key_hash = $1
        """,
        candidate_hash
    )
