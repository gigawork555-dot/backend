# tests/api_test_auth_routes.py
"""
Coverage target: app/auth/routes.py (+ app/auth/dependencies.py integration)
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

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TEST_DIR = os.path.dirname(__file__)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from conftest import check, check_is, check_approx  # noqa: E402

from app.auth import routes as auth_routes          # noqa: E402
from app.auth.dependencies import (                 # noqa: E402
    hash_password,
    get_current_user_jwt,
    require_admin,
)
from app.database import get_db_pool                # noqa: E402


# =================================================================
# Fixtures
# =================================================================

def _make_pool(conn):
    pool = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


@pytest.fixture
def conn():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    return conn


@pytest.fixture
def app_with_pool(conn):
    pool = _make_pool(conn)
    app = FastAPI()
    app.include_router(auth_routes.router)

    async def _override_pool():
        return pool

    app.dependency_overrides[get_db_pool] = _override_pool
    return app, conn


@pytest.fixture
def client(app_with_pool):
    app, _ = app_with_pool
    return TestClient(app)


# =================================================================
# POST /auth/login
# =================================================================

def test_login_success_full_roundtrip(client, conn):
    real_hash = hash_password("secret123")
    conn.fetchrow = AsyncMock(return_value={
        "id": 1, "username": "thaitanawut", "hashed_password": real_hash,
        "role": "admin", "is_active": True,
    })

    resp = client.post(
        "/auth/login",
        data={"username": "thaitanawut", "password": "secret123"},
    )

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['token_type']", body["token_type"], "bearer")
    check("body['username']", body["username"], "thaitanawut")
    check("body['role']", body["role"], "admin")
    print(f"  🔎 {'access_token non-empty':<28} -> actual={bool(body['access_token'])}")
    assert body["access_token"]


def test_login_wrong_password_returns_401(client, conn):
    real_hash = hash_password("correct-password")
    conn.fetchrow = AsyncMock(return_value={
        "id": 1, "username": "thaitanawut", "hashed_password": real_hash,
        "role": "user", "is_active": True,
    })

    resp = client.post(
        "/auth/login",
        data={"username": "thaitanawut", "password": "wrong-password"},
    )

    check("resp.status_code (wrong password)", resp.status_code, 401)


def test_login_unknown_username_returns_401(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.post(
        "/auth/login",
        data={"username": "ghost", "password": "whatever"},
    )

    check("resp.status_code (unknown user)", resp.status_code, 401)


def test_login_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.post(
        "/auth/login",
        data={"username": "thaitanawut", "password": "secret123"},
    )

    check("resp.status_code (db error)", resp.status_code, 500)


def test_login_missing_form_fields_returns_422(client):
    resp = client.post("/auth/login", data={})
    check("resp.status_code (missing fields)", resp.status_code, 422)


# =================================================================
# GET /auth/me
# =================================================================

def test_get_me_returns_current_user(app_with_pool):
    app, _ = app_with_pool

    async def _fake_current_user():
        return {"user_id": 1, "username": "thaitanawut", "role": "admin"}

    app.dependency_overrides[get_current_user_jwt] = _fake_current_user
    client = TestClient(app)

    resp = client.get("/auth/me")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['username']", body["username"], "thaitanawut")
    check("body['role']", body["role"], "admin")


def test_get_me_without_token_returns_401(client):
    resp = client.get("/auth/me")
    check("resp.status_code (no token)", resp.status_code, 401)


# =================================================================
# POST /auth/register
# =================================================================

def test_register_user_success_as_admin(app_with_pool):
    app, conn = app_with_pool

    async def _fake_admin():
        return {"user_id": 1, "username": "admin", "role": "admin"}

    app.dependency_overrides[require_admin] = _fake_admin
    client = TestClient(app)

    conn.fetchrow = AsyncMock(side_effect=[
        None,
        {"id": 2, "username": "newintern", "full_name": "New Intern",
         "role": "viewer", "created_at": None},
    ])

    resp = client.post(
        "/auth/register",
        json={"username": "newintern", "password": "pass1234",
              "full_name": "New Intern", "role": "viewer"},
    )

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['user']['username']", body["user"]["username"], "newintern")


def test_register_user_duplicate_username_returns_400(app_with_pool):
    app, conn = app_with_pool

    async def _fake_admin():
        return {"user_id": 1, "username": "admin", "role": "admin"}

    app.dependency_overrides[require_admin] = _fake_admin
    client = TestClient(app)

    conn.fetchrow = AsyncMock(return_value={
        "id": 5, "username": "newintern", "role": "viewer",
        "is_active": True,
    })

    resp = client.post(
        "/auth/register",
        json={"username": "newintern", "password": "pass1234"},
    )

    check("resp.status_code (duplicate)", resp.status_code, 400)


def test_register_user_non_admin_rejected(app_with_pool):
    app, _ = app_with_pool

    async def _fake_non_admin():
        return {"user_id": 3, "username": "viewer1", "role": "viewer"}

    app.dependency_overrides[get_current_user_jwt] = _fake_non_admin
    client = TestClient(app)

    resp = client.post(
        "/auth/register",
        json={"username": "someone", "password": "pass1234"},
    )

    check("resp.status_code (non-admin)", resp.status_code, 403)


def test_register_user_no_auth_token_returns_401(client):
    resp = client.post(
        "/auth/register",
        json={"username": "someone", "password": "pass1234"},
    )
    check("resp.status_code (no token)", resp.status_code, 401)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))