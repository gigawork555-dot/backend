# app/database.py

"""
Database Layer

Centralized asyncpg Pool Manager
for FastAPI + MQTT + TimescaleDB

Responsibilities:
- Create asyncpg connection pool
- Share pool across application
- Health check
- Graceful shutdown
- Production ready
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

# Shared Pool Reference
_db_pool: Optional[asyncpg.Pool] = None

# Prevent duplicate pool creation
_pool_lock = asyncio.Lock()


async def create_db_pool() -> asyncpg.Pool:
    """
    Create and initialize asyncpg pool.

    Returns:
        asyncpg.Pool
    """

    global _db_pool

    if _db_pool is not None:
        logger.info("Database pool already exists")
        return _db_pool

    async with _pool_lock:

        # Double-check after acquiring lock
        if _db_pool is not None:
            return _db_pool

        try:
            logger.info("Creating database pool...")

            _db_pool = await asyncpg.create_pool(
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                user=settings.DB_USER,
                password=settings.DB_PASS,
                database=settings.DB_NAME,
                min_size=5,
                max_size=20,
                timeout=10,
                command_timeout=30,
            )

            logger.info(
                "Database pool created successfully"
            )

            return _db_pool

        except Exception:
            logger.error(
                "Database connection failed",
                exc_info=True
            )
            raise


async def get_db_pool() -> asyncpg.Pool:
    """
    Return existing database pool.

    Raises:
        RuntimeError:
            If pool is not initialized.
    """

    if _db_pool is None:
        raise RuntimeError(
            "Database pool is not initialized. "
            "Call create_db_pool() first."
        )

    return _db_pool


async def check_db_health() -> bool:
    """
    Verify database connectivity.

    Returns:
        bool:
            True if database is healthy.
            False otherwise.
    """

    try:
        pool = await get_db_pool()

        async with pool.acquire() as conn:
            await conn.fetchval(
                "SELECT 1"
            )

        return True

    except Exception:
        logger.error(
            "Database health check failed",
            exc_info=True
        )

        return False


async def close_db_pool() -> None:
    """
    Gracefully close shared database pool.
    """

    global _db_pool

    if _db_pool is None:
        logger.info(
            "Database pool already closed"
        )
        return

    try:
        logger.info(
            "Closing database pool..."
        )

        await _db_pool.close()

        logger.info(
            "Database pool closed successfully"
        )

    except Exception:
        logger.error(
            "Error while closing database pool",
            exc_info=True
        )

    finally:
        _db_pool = None

async def get_pool():
    return await get_db_pool()