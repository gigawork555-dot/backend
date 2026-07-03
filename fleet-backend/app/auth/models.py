# app/auth/models.py

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
# API KEYS
# =====================================================

async def create_api_key(
    conn: asyncpg.Connection,
    key_name: str,
) -> dict:

    key_hash = secrets.token_hex(32)

    row = await conn.fetchrow(
        """
        INSERT INTO api_keys
        (
            name,
            key_hash,
            is_active
        )
        VALUES
        (
            $1,
            $2,
            TRUE
        )
        RETURNING *
        """,
        key_name,
        key_hash
    )

    return dict(row)


async def get_api_key(
    conn: asyncpg.Connection,
    api_key: str
) -> Optional[dict]:

    row = await conn.fetchrow(
        """
        SELECT *
        FROM api_keys
        WHERE key_hash = $1
          AND is_active = TRUE
        """,
        api_key
    )

    return dict(row) if row else None


async def list_api_keys(
    conn: asyncpg.Connection
) -> list:

    rows = await conn.fetch(
        """
        SELECT
            id,
            name,
            is_active,
            created_at
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
        SET is_active = FALSE
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
    """
    await conn.execute(
        """
        UPDATE api_keys
        SET last_used = NOW()
        WHERE key_hash = $1
        """,
        api_key
    )