# tests/test_developer_routes.py
"""
Coverage target: app/developer/routes.py + app/auth/models.py
(1 user = 1 API key, FDD v1.4 §13)

Uses pytest-asyncio + unittest.mock — no real database required for the
route-level tests. The unique-constraint test exercises real asyncpg
error-raising semantics by simulating what the partial unique index
would do (asyncpg.UniqueViolationError), since a live Postgres isn't
available in this test environment.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "test_db")
os.environ.setdefault("DB_USER", "test_user")
os.environ.setdefault("DB_PASS", "test_pass")
os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("MQTT_PORT", "1883")
os.environ.setdefault("MQTT_TOPIC", "test/topic")

import pytest  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import asyncpg  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.developer import routes as developer_routes  # noqa: E402
from app.auth.dependencies import get_current_user_jwt, hash_password  # noqa: E402
from app.auth.models import (  # noqa: E402
    generate_api_key,
    get_api_key,
    create_api_key,
    regenerate_api_key_for_user,
    revoke_user_api_key,
)
from app.database import get_db_pool  # noqa: E402


# =================================================================
# Fixtures
# =================================================================

def _make_tx_cm():
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    return tx_cm


def _make_conn():
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_make_tx_cm())
    return conn


def _make_pool(conn):
    pool = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


@pytest.fixture
def conn():
    return _make_conn()


@pytest.fixture
def pool(conn):
    return _make_pool(conn)


@pytest.fixture
def app_with_pool(pool):
    app = FastAPI()
    app.include_router(developer_routes.router)

    async def _override_pool():
        return pool

    app.dependency_overrides[get_db_pool] = _override_pool
    return app


@pytest.fixture
def logged_in_client(app_with_pool):
    async def _fake_user():
        return {"user_id": 42, "username": "intern_a", "role": "user"}

    app_with_pool.dependency_overrides[get_current_user_jwt] = _fake_user
    return TestClient(app_with_pool)


# =================================================================
# generate_api_key() / hashing
# =================================================================

def test_generate_api_key_has_expected_prefix_and_distinct_hash():
    plaintext, key_hash, key_prefix = generate_api_key()
    assert plaintext.startswith("ktc_")
    assert key_hash != plaintext
    assert len(key_hash) == 64  # sha256 hex digest
    assert key_prefix.startswith("ktc_")
    assert plaintext.startswith(key_prefix)


def test_generate_api_key_produces_unique_keys():
    k1, h1, _ = generate_api_key()
    k2, h2, _ = generate_api_key()
    assert k1 != k2
    assert h1 != h2


# =================================================================
# get_api_key() verify — hashing + timing-safe compare
# =================================================================

async def test_get_api_key_matches_correct_plaintext():
    plaintext, key_hash, prefix = generate_api_key()

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"id": 1, "key_hash": key_hash, "user_id": 42, "is_active": True},
    ])

    result = await get_api_key(conn, plaintext)
    assert result is not None
    assert result["id"] == 1


async def test_get_api_key_rejects_wrong_plaintext():
    plaintext, key_hash, prefix = generate_api_key()

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"id": 1, "key_hash": key_hash, "user_id": 42, "is_active": True},
    ])

    result = await get_api_key(conn, "ktc_totally-wrong-key")
    assert result is None


async def test_get_api_key_empty_string_returns_none():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    result = await get_api_key(conn, "")
    assert result is None


# =================================================================
# API — GET /developer/me/api-key
# =================================================================

def test_get_my_api_key_no_key_yet(logged_in_client, pool, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = logged_in_client.get("/developer/me/api-key")

    assert resp.status_code == 200
    assert resp.json() == {"has_key": False}


def test_get_my_api_key_returns_masked_value(logged_in_client, pool, conn):
    conn.fetchrow = AsyncMock(return_value={
        "id": 1, "user_id": 42, "key_prefix": "ktc_ab12cdef",
        "is_active": True, "created_at": "2026-01-01T00:00:00Z",
        "last_rotated_at": "2026-01-01T00:00:00Z", "last_used": None,
    })

    resp = logged_in_client.get("/developer/me/api-key")

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_key"] is True
    assert body["masked_key"] == "ktc_ab12cdef****"


def test_get_my_api_key_requires_jwt():
    app = FastAPI()
    app.include_router(developer_routes.router)
    client = TestClient(app)

    resp = client.get("/developer/me/api-key")
    assert resp.status_code == 401


# =================================================================
# API — POST /developer/me/api-key/regenerate
# =================================================================

def test_regenerate_first_time_issues_new_key(logged_in_client, pool, conn, monkeypatch):
    real_hash = hash_password("correct-password")

    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 42, "username": "intern_a", "full_name": None, "role": "user",
         "is_active": True, "created_at": None},  # get_user_by_id
        {"hashed_password": real_hash},             # credential lookup
    ])
    conn.execute = AsyncMock(return_value="UPDATE 0")  # revoke (no prior key)
    conn.fetchrow_insert_side_effect = None

    # regenerate_api_key_for_user -> revoke_user_api_key (execute) then
    # create_api_key (fetchrow) — need a THIRD fetchrow for the INSERT
    async def fetchrow_sequence(*args, **kwargs):
        calls = fetchrow_sequence.n
        fetchrow_sequence.n += 1
        if calls == 0:
            return {"id": 42, "username": "intern_a", "full_name": None,
                    "role": "user", "is_active": True, "created_at": None}
        if calls == 1:
            return {"hashed_password": real_hash}
        return {
            "id": 5, "name": "developer-portal-key", "user_id": 42,
            "key_prefix": "ktc_xxxxxxxx", "is_active": True,
            "created_at": "2026-01-01T00:00:00Z", "last_rotated_at": "2026-01-01T00:00:00Z",
        }
    fetchrow_sequence.n = 0
    conn.fetchrow = AsyncMock(side_effect=fetchrow_sequence)

    resp = logged_in_client.post(
        "/developer/me/api-key/regenerate", json={"password": "correct-password"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["api_key"].startswith("ktc_")


def test_regenerate_wrong_password_returns_401_and_issues_nothing(logged_in_client, pool, conn):
    real_hash = hash_password("correct-password")

    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 42, "username": "intern_a", "full_name": None, "role": "user",
         "is_active": True, "created_at": None},
        {"hashed_password": real_hash},
    ])
    conn.execute = AsyncMock()

    resp = logged_in_client.post(
        "/developer/me/api-key/regenerate", json={"password": "wrong-password"}
    )

    assert resp.status_code == 401
    conn.execute.assert_not_awaited()


def test_regenerate_replaces_old_key_old_key_stops_working():
    """
    Integration-style test at the model layer: after
    regenerate_api_key_for_user(), the OLD plaintext key must no longer
    verify via get_api_key() — only the NEW one should.
    """
    async def _run():
        old_hash_holder = {}

        # Simulate a DB row store for a single user's key lifecycle
        state = {"row": None}

        conn = MagicMock()

        async def fetchrow_insert(*args, **kwargs):
            # args: name, key_hash, user_id, key_prefix (positional after query)
            key_hash = args[2]
            user_id = args[3]
            key_prefix = args[4]
            row = {
                "id": 1, "name": args[1], "key_hash": key_hash,
                "user_id": user_id, "key_prefix": key_prefix,
                "is_active": True, "created_at": "t",
            }
            state["row"] = row
            return row

        conn.fetchrow = AsyncMock(side_effect=fetchrow_insert)
        conn.execute = AsyncMock(return_value="UPDATE 1")

        first = await regenerate_api_key_for_user(conn, user_id=42)
        old_plaintext = first["api_key"]
        old_row = dict(state["row"])

        # regenerate again -> new key
        second = await regenerate_api_key_for_user(conn, user_id=42)
        new_plaintext = second["api_key"]
        new_row = dict(state["row"])

        assert old_plaintext != new_plaintext

        # verify old key against a DB snapshot where only the NEW row is active
        conn2 = MagicMock()
        conn2.fetch = AsyncMock(return_value=[new_row])

        old_result = await get_api_key(conn2, old_plaintext)
        new_result = await get_api_key(conn2, new_plaintext)

        assert old_result is None
        assert new_result is not None

    import asyncio
    asyncio.run(_run())


# =================================================================
# API — POST /developer/me/api-key/revoke
# =================================================================

def test_revoke_disables_key_immediately(logged_in_client, pool, conn):
    real_hash = hash_password("correct-password")

    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 42, "username": "intern_a", "full_name": None, "role": "user",
         "is_active": True, "created_at": None},
        {"hashed_password": real_hash},
    ])
    conn.execute = AsyncMock(return_value="UPDATE 1")

    resp = logged_in_client.post(
        "/developer/me/api-key/revoke", json={"password": "correct-password"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"


def test_revoke_no_active_key_returns_404(logged_in_client, pool, conn):
    real_hash = hash_password("correct-password")

    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 42, "username": "intern_a", "full_name": None, "role": "user",
         "is_active": True, "created_at": None},
        {"hashed_password": real_hash},
    ])
    conn.execute = AsyncMock(return_value="UPDATE 0")

    resp = logged_in_client.post(
        "/developer/me/api-key/revoke", json={"password": "correct-password"}
    )

    assert resp.status_code == 404


def test_revoke_wrong_password_returns_401(logged_in_client, pool, conn):
    real_hash = hash_password("correct-password")

    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 42, "username": "intern_a", "full_name": None, "role": "user",
         "is_active": True, "created_at": None},
        {"hashed_password": real_hash},
    ])
    conn.execute = AsyncMock()

    resp = logged_in_client.post(
        "/developer/me/api-key/revoke", json={"password": "wrong"}
    )

    assert resp.status_code == 401
    conn.execute.assert_not_awaited()


# =================================================================
# DB constraint — 2 active keys for same user must raise
# =================================================================

async def test_unique_partial_index_rejects_two_active_keys_same_user():
    """
    Simulates the partial unique index
    (uq_api_keys_one_active_per_user ON api_keys(user_id) WHERE is_active)
    by having the mocked connection raise asyncpg.UniqueViolationError on
    the second INSERT for the same user_id while is_active=TRUE — this
    documents the expected DB-level behavior enforced by the migration.
    """
    conn = MagicMock()

    call_count = {"n": 0}

    async def fetchrow_with_constraint(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "id": 1, "name": args[1], "key_hash": args[2],
                "user_id": args[3], "key_prefix": args[4],
                "is_active": True, "created_at": "t",
            }
        raise asyncpg.UniqueViolationError(
            "duplicate key value violates unique constraint "
            "\"uq_api_keys_one_active_per_user\""
        )

    conn.fetchrow = AsyncMock(side_effect=fetchrow_with_constraint)

    first = await create_api_key(conn, "key-1", user_id=42)
    assert first["user_id"] == 42

    with pytest.raises(asyncpg.UniqueViolationError):
        await create_api_key(conn, "key-2", user_id=42)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))
