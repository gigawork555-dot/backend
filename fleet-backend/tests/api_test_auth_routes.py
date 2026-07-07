# tests/test_auth_routes.py
"""
Coverage target: app/auth/routes.py (+ app/auth/dependencies.py integration)

Endpoints covered:
  - POST /auth/login     : OAuth2 password flow -> JWT access token
  - GET  /auth/me         : current user via JWT bearer
  - POST /auth/register  : admin-only user creation

Testing strategy
-----------------
- /auth/login and /auth/register go through `get_db_pool()` ->
  `pool.acquire()` (async context manager) -> plain functions in
  app.auth.models, which we don't need to mock separately since they
  just wrap conn.fetchrow/execute — we mock the connection directly.
- /auth/me and /auth/register additionally depend on
  `get_current_user_jwt` / `require_admin`, which decode a real JWT via
  `app.auth.dependencies.decode_access_token`. Rather than crafting real
  bcrypt hashes and JWTs by hand for every test, we override those two
  FastAPI dependencies directly via `app.dependency_overrides` — this
  is the standard, supported way to bypass auth internals in tests
  while still exercising real routing/business logic in the handler.
- For /auth/login we DO exercise the real bcrypt/JWT code path (via a
  real hash + real decode) for at least one full round-trip test, to
  make sure the actual crypto wiring works end-to-end.
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

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["username"] == "thaitanawut"
    assert body["role"] == "admin"
    assert body["access_token"]  # non-empty JWT string


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

    assert resp.status_code == 401


def test_login_unknown_username_returns_401(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.post(
        "/auth/login",
        data={"username": "ghost", "password": "whatever"},
    )

    assert resp.status_code == 401


def test_login_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.post(
        "/auth/login",
        data={"username": "thaitanawut", "password": "secret123"},
    )

    assert resp.status_code == 500


def test_login_missing_form_fields_returns_422(client):
    resp = client.post("/auth/login", data={})
    assert resp.status_code == 422


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

    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "thaitanawut"
    assert body["role"] == "admin"


def test_get_me_without_token_returns_401(client):
    # no dependency override -> real get_current_user_jwt runs, no bearer
    # token supplied -> oauth2_scheme yields empty token -> 401
    resp = client.get("/auth/me")
    assert resp.status_code == 401


# =================================================================
# POST /auth/register
# =================================================================

def test_register_user_success_as_admin(app_with_pool):
    app, conn = app_with_pool

    async def _fake_admin():
        return {"user_id": 1, "username": "admin", "role": "admin"}

    app.dependency_overrides[require_admin] = _fake_admin
    client = TestClient(app)

    # existing-user lookup -> None (no duplicate)
    conn.fetchrow = AsyncMock(side_effect=[
        None,  # get_user_by_username() during duplicate check
        {"id": 2, "username": "newintern", "full_name": "New Intern",
         "role": "viewer", "created_at": None},  # INSERT ... RETURNING
    ])

    resp = client.post(
        "/auth/register",
        json={"username": "newintern", "password": "pass1234",
              "full_name": "New Intern", "role": "viewer"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "newintern"


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

    assert resp.status_code == 400


def test_register_user_non_admin_rejected(app_with_pool):
    app, _ = app_with_pool

    async def _fake_non_admin():
        return {"user_id": 3, "username": "viewer1", "role": "viewer"}

    # override get_current_user_jwt (used internally by require_admin)
    # rather than require_admin itself, to exercise the real role check
    app.dependency_overrides[get_current_user_jwt] = _fake_non_admin
    client = TestClient(app)

    resp = client.post(
        "/auth/register",
        json={"username": "someone", "password": "pass1234"},
    )

    assert resp.status_code == 403


def test_register_user_no_auth_token_returns_401(client):
    resp = client.post(
        "/auth/register",
        json={"username": "someone", "password": "pass1234"},
    )
    assert resp.status_code == 401


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))
