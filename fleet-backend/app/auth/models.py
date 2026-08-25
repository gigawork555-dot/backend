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
    """
    ใช้ตอน login เท่านั้น — WHERE is_active = TRUE บล็อกบัญชีที่ยัง
    pending (รอ admin อนุมัติ) ไม่ให้ login ได้โดยอัตโนมัติ ไม่ต้อง
    เขียน logic เช็ค pending แยกที่ /auth/login เลย
    """

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


async def get_user_by_username_any_status(
    conn: asyncpg.Connection,
    username_or_email: str,
) -> Optional[dict]:
    """
    เหมือน get_user_by_username() แต่ไม่กรอง is_active — ใช้ตอน
    signup เพื่อเช็คว่า username/email ซ้ำหรือไม่ (รวมทั้งบัญชีที่
    pending อยู่ด้วย ไม่ใช่แค่บัญชี active)
    """
    row = await conn.fetchrow(
        "SELECT * FROM users WHERE username = $1 OR email = $1",
        username_or_email,
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
# Public self-signup (pending approval)
# =====================================================

async def create_pending_user(
    conn: asyncpg.Connection,
    username: str,
    email: str,
    hashed_password: str,
    full_name: Optional[str] = None,
) -> dict:
    """
    สมัครสมาชิกด้วยตัวเอง — is_active = FALSE เสมอ, role = 'user' เสมอ
    (ไม่มีทางสมัครเป็น admin เองผ่าน endpoint นี้)
    """
    row = await conn.fetchrow(
        """
        INSERT INTO users
            (username, email, hashed_password, full_name, is_active, role)
        VALUES
            ($1, $2, $3, $4, FALSE, 'user')
        RETURNING id, username, email, full_name, role, is_active, created_at
        """,
        username, email, hashed_password, full_name,
    )
    return dict(row)


# =====================================================
# Admin — approval queue
# =====================================================

async def list_pending_users(conn: asyncpg.Connection) -> list:
    rows = await conn.fetch(
        """
        SELECT id, username, email, full_name, role, created_at
        FROM users
        WHERE is_active = FALSE
        ORDER BY created_at ASC
        """
    )
    return [dict(r) for r in rows]


async def approve_user(conn: asyncpg.Connection, user_id: int) -> Optional[dict]:
    row = await conn.fetchrow(
        """
        UPDATE users
        SET is_active = TRUE
        WHERE id = $1 AND is_active = FALSE
        RETURNING id, username, email, full_name, role, is_active, created_at
        """,
        user_id,
    )
    return dict(row) if row else None


# =====================================================
# Admin — full user + key management (list, revoke, delete)
# =====================================================

async def list_all_users_with_key_status(conn: asyncpg.Connection) -> list:
    """
    รายชื่อ user ทั้งหมด + สถานะ key ปัจจุบัน (สำหรับหน้า Admin Dashboard)
    """
    rows = await conn.fetch(
        """
        SELECT
            u.id, u.username, u.email, u.full_name, u.role,
            u.is_active AS account_active,
            u.created_at,
            k.id            AS key_id,
            k.key_prefix,
            k.is_active     AS key_active,
            k.created_at    AS key_created_at,
            k.revoked_at
        FROM users u
        LEFT JOIN api_keys k
               ON k.user_id = u.id
              AND k.is_active = TRUE
        ORDER BY u.created_at DESC
        """
    )
    return [dict(r) for r in rows]


async def delete_user(conn: asyncpg.Connection, user_id: int) -> bool:
    """
    ลบ user ทิ้งถาวร — api_keys ของ user นี้ถูกลบตามไปด้วยอัตโนมัติ
    (FK: api_keys.user_id REFERENCES users(id) ON DELETE CASCADE,
    ดู docker/postgres/init.sql) เพื่อให้ user ต้องสมัครใหม่ + ได้
    key ใหม่ ตามดีไซน์ "generate key ได้ครั้งเดียวต่อ user"
    """
    result = await conn.execute(
        "DELETE FROM users WHERE id = $1",
        user_id,
    )
    try:
        return int(result.split()[-1]) > 0
    except (ValueError, IndexError, AttributeError):
        return False


async def admin_revoke_key_for_user(
    conn: asyncpg.Connection,
    user_id: int,
) -> bool:
    """
    Admin สั่ง revoke key ของ user คนหนึ่ง — key หยุดใช้งานได้ทันที
    แต่ account ของ user ยังอยู่ ไม่ถูกลบ — user คนนี้ล็อกอินเข้า
    dashboard ได้ปกติ เห็นสถานะ "key ถูกระงับ" แต่ generate key ใหม่
    เองไม่ได้ (ตามดีไซน์ generate-once) ต้องให้ admin delete user
    แล้วสมัครใหม่เท่านั้นถึงจะได้ key ใหม่
    """
    return await revoke_user_api_key(conn, user_id)


# =====================================================
# API KEYS — 1 user = 1 active key (FDD §13)
#
# [SECURITY] key_hash เก็บ SHA-256 hash เท่านั้น ไม่เคยเก็บ plaintext
# ลง DB — plaintext แสดงให้ user เห็น "ครั้งเดียว" ตอนสร้างเท่านั้น
# verify (get_api_key) ต้อง hash ค่าที่รับมาก่อนเทียบด้วย timing-safe
# compare (hmac.compare_digest)
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
    user_id เป็น optional parameter สำหรับ flow "1 user = 1 key"
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

    ต้อง hash ค่าที่รับเข้ามาก่อนเทียบกับ DB เสมอ ห้ามเทียบ plaintext
    กับ column ตรงๆ — ใช้ timing-safe compare (hmac.compare_digest)
    เพื่อป้องกัน timing attack
    """
    if not api_key:
        return None

    candidate_hash = _hash_api_key(api_key)

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
    [เก็บไว้เผื่อใช้งานฝั่ง admin ในอนาคต — ไม่ได้ผูกกับ endpoint
    self-service ของ user แล้ว เพราะ user generate ได้ครั้งเดียว]

    Revoke key เก่า (ถ้ามี) แล้วออก key ใหม่ให้ user คนนี้ ภายใน
    transaction เดียวกัน (caller ต้องเปิด conn.transaction() ครอบ)
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

    ต้อง hash api_key ก่อน UPDATE WHERE เช่นกัน เพราะ key_hash column
    เก็บ SHA-256 hash ไม่ใช่ plaintext
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