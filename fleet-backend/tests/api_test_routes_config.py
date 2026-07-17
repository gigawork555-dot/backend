# tests/api_test_routes_config.py
"""
Coverage target: app/api/routes_config.py
... (docstring เดิมคงไว้) ...
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

from app.api import routes_config          # noqa: E402
from app.database import get_db_pool       # noqa: E402
from app.api.routes_config import (        # noqa: E402
    _validate_device_id_format,
)


# =================================================================
# Fixtures — Mock asyncpg Pool / Connection / Transaction
# =================================================================

def _make_tx_cm():
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    return tx_cm


def _make_conn(fetchrow_side_effect=None, fetchrow_return=None,
               execute_return="INSERT 0 1"):
    conn = MagicMock()

    if fetchrow_side_effect is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    else:
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)

    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=execute_return)
    conn.transaction = MagicMock(return_value=_make_tx_cm())

    return conn


def _make_pool(conn):
    pool = MagicMock()

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)

    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock(return_value="INSERT 0 1")

    return pool


@pytest.fixture
def conn():
    return _make_conn()


@pytest.fixture
def pool(conn):
    return _make_pool(conn)


@pytest.fixture
def client(pool):
    app = FastAPI()
    app.include_router(routes_config.router)

    async def _override_get_db_pool():
        return pool

    app.dependency_overrides[get_db_pool] = _override_get_db_pool

    return TestClient(app)


# =================================================================
# _validate_device_id_format() — หน่วยทดสอบระดับฟังก์ชันล้วน
# =================================================================

def test_validate_device_id_format_accepts_valid_format():
    result = _validate_device_id_format("KTC-001")
    check("_validate_device_id_format('KTC-001')", result, "KTC-001")


def test_validate_device_id_format_normalizes_lowercase_to_uppercase():
    result = _validate_device_id_format("ktc-001")
    check("_validate_device_id_format('ktc-001')", result, "KTC-001")


def test_validate_device_id_format_strips_whitespace():
    result = _validate_device_id_format("  KTC-001  ")
    check("_validate_device_id_format('  KTC-001  ')", result, "KTC-001")


def test_validate_device_id_format_none_passthrough():
    result = _validate_device_id_format(None)
    check_is("_validate_device_id_format(None)", result, None)


def test_validate_device_id_format_rejects_numeric_only():
    with pytest.raises(ValueError, match="KTC-XXX"):
        _validate_device_id_format("1")
    print("  🔎 ValueError raised (numeric-only '1') -> actual=True expected=True ✅")


def test_validate_device_id_format_rejects_wrong_digit_count():
    with pytest.raises(ValueError):
        _validate_device_id_format("KTC-01")
    print("  🔎 ValueError raised (wrong digit count) -> actual=True expected=True ✅")


def test_validate_device_id_format_rejects_missing_prefix():
    with pytest.raises(ValueError):
        _validate_device_id_format("XYZ-001")
    print("  🔎 ValueError raised (missing prefix)   -> actual=True expected=True ✅")


def test_validate_device_id_format_error_message_includes_field_name():
    with pytest.raises(ValueError, match="new_device_id"):
        _validate_device_id_format("bad", field_name="new_device_id")
    print("  🔎 ValueError message includes field_name -> actual=True expected=True ✅")


# =================================================================
# GET /api/v1/devices
# =================================================================

def test_get_devices_returns_list(client, pool):
    pool.fetch = AsyncMock(return_value=[
        {"id": "KTC-001", "vehicle_id": 101, "active": True, "registered_at": None},
        {"id": "KTC-002", "vehicle_id": None, "active": True, "registered_at": None},
    ])

    resp = client.get("/api/v1/devices")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['total']", body["total"], 2)
    check("len(body['devices'])", len(body["devices"]), 2)


def test_get_devices_empty_list(client, pool):
    pool.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/devices")

    check("resp.status_code", resp.status_code, 200)
    check("resp.json()", resp.json(), {"total": 0, "devices": []})


def test_get_devices_db_error_returns_500(client, pool):
    pool.fetch = AsyncMock(side_effect=RuntimeError("connection lost"))

    resp = client.get("/api/v1/devices")

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# GET /api/v1/config_device
# =================================================================

def test_get_device_config_found_and_bound(client, pool):
    pool.fetchrow = AsyncMock(return_value={
        "device_id": "KTC-001",
        "vehicle_id": 101,
        "active": True,
        "date_update_latest": None,
    })

    resp = client.get("/api/v1/config_device", params={"device_id": "KTC-001"})

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check_is("body['is_bound']", body["is_bound"], True)
    check("body['status']", body["status"], "active")


def test_get_device_config_found_but_unbound(client, pool):
    pool.fetchrow = AsyncMock(return_value={
        "device_id": "KTC-002",
        "vehicle_id": None,
        "active": False,
        "date_update_latest": None,
    })

    resp = client.get("/api/v1/config_device", params={"device_id": "KTC-002"})

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check_is("body['is_bound']", body["is_bound"], False)
    check("body['status']", body["status"], "inactive")


def test_get_device_config_not_found_returns_404(client, pool):
    pool.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/config_device", params={"device_id": "KTC-999"})

    check("resp.status_code (not found)", resp.status_code, 404)


def test_get_device_config_invalid_format_returns_422(client):
    resp = client.get("/api/v1/config_device", params={"device_id": "1"})

    check("resp.status_code (invalid format)", resp.status_code, 422)


def test_get_device_config_db_error_returns_500(client, pool):
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.get("/api/v1/config_device", params={"device_id": "KTC-001"})

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# POST /api/v1/config_device/register — single device
# =================================================================

def _valid_register_payload(device_id="KTC-001", vehicle_id=101):
    return {
        "device_id": device_id,
        "device_name": "Test Device",
        "vehicle_id": vehicle_id,
    }


def test_register_device_single_success(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    check("resp.status_code", resp.status_code, 201)
    body = resp.json()
    check("body['status']", body["status"], "success")
    check("body['device_id']", body["device_id"], "KTC-001")
    check("body['vehicle_id']", body["vehicle_id"], 101)


def test_register_device_single_invalid_format_returns_422(client):
    resp = client.post(
        "/api/v1/config_device/register",
        json=_valid_register_payload(device_id="1"),
    )

    check("resp.status_code (invalid format)", resp.status_code, 422)


def test_register_device_conflict_exact_same_binding_returns_409(client, conn):
    conn.fetchrow = AsyncMock(return_value={"vehicle_id": 101})

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    check("resp.status_code (exact binding conflict)", resp.status_code, 409)
    detail = resp.json()["detail"]
    print(f"  🔎 {'detail contains msg':<28} -> actual={detail!r}")
    assert "already bound to vehicle" in detail


def test_register_device_conflict_bound_to_different_vehicle_returns_409(client, conn):
    conn.fetchrow = AsyncMock(side_effect=[
        None,
        {"vehicle_id": 202},
    ])

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    check("resp.status_code (bound to other vehicle)", resp.status_code, 409)
    detail = resp.json()["detail"]
    print(f"  🔎 {'detail contains vehicle 202':<28} -> actual={detail!r}")
    assert "already bound to vehicle 202" in detail


def test_register_device_conflict_vehicle_already_has_other_device_returns_409(client, conn):
    conn.fetchrow = AsyncMock(side_effect=[
        None,
        None,
        {"device_id": "KTC-999"},
    ])

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    check("resp.status_code (vehicle has other device)", resp.status_code, 409)
    detail = resp.json()["detail"]
    print(f"  🔎 {'detail contains KTC-999':<28} -> actual={detail!r}")
    assert "already bound to device KTC-999" in detail


def test_register_device_all_checks_pass_then_db_insert_fails_returns_500(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(side_effect=RuntimeError("insert failed"))

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    check("resp.status_code (insert failed)", resp.status_code, 500)


def test_register_device_single_pool_acquire_itself_fails_returns_500(client, pool):
    class _BrokenAcquire:
        def __call__(self):
            raise RuntimeError("pool exhausted")

    pool.acquire = _BrokenAcquire()

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    check("resp.status_code (acquire broken)", resp.status_code, 500)


# =================================================================
# POST /api/v1/config_device/register/batch
# =================================================================

def test_register_device_batch_success(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    payload = {
        "devices": [
            _valid_register_payload("KTC-001", 101),
            _valid_register_payload("KTC-002", 102),
        ]
    }

    resp = client.post("/api/v1/config_device/register/batch", json=payload)

    check("resp.status_code", resp.status_code, 201)
    body = resp.json()
    check("body['registered']", body["registered"], 2)
    check("len(body['results'])", len(body["results"]), 2)


def test_register_device_batch_empty_list_returns_400(client):
    resp = client.post("/api/v1/config_device/register/batch", json={"devices": []})

    check("resp.status_code (empty batch)", resp.status_code, 400)


def test_register_device_batch_conflict_rolls_back_entire_batch(client, conn):
    conn.fetchrow = AsyncMock(side_effect=[
        None, None, None,
        {"vehicle_id": 999},
    ])

    payload = {
        "devices": [
            _valid_register_payload("KTC-001", 101),
            _valid_register_payload("KTC-002", 102),
        ]
    }

    resp = client.post("/api/v1/config_device/register/batch", json=payload)

    check("resp.status_code (batch rollback)", resp.status_code, 409)


def test_register_device_batch_pool_acquire_itself_fails_returns_500(client, pool):
    class _BrokenAcquire:
        def __call__(self):
            raise RuntimeError("pool exhausted")

    pool.acquire = _BrokenAcquire()

    payload = {"devices": [_valid_register_payload("KTC-001", 101)]}

    resp = client.post("/api/v1/config_device/register/batch", json=payload)

    check("resp.status_code (acquire broken)", resp.status_code, 500)


def test_register_device_batch_invalid_device_id_in_list_returns_422(client):
    payload = {
        "devices": [
            _valid_register_payload("KTC-001", 101),
            _valid_register_payload("BAD", 102),
        ]
    }

    resp = client.post("/api/v1/config_device/register/batch", json=payload)

    check("resp.status_code (invalid id in batch)", resp.status_code, 422)


# =================================================================
# PUT /api/v1/config/vehicle
# =================================================================

def _vehicle_update_payload(vehicle_id=101, new_device_id="KTC-001",
                            old_device_id=None, driver_id=None):
    payload = {
        "vehicle_id": vehicle_id,
        "new_device_id": new_device_id,
    }
    if old_device_id is not None:
        payload["old_device_id"] = old_device_id
    if driver_id is not None:
        payload["driver_id"] = driver_id
    return payload


def test_update_vehicle_config_registers_new_binding_when_vehicle_has_no_device(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.put("/api/v1/config/vehicle", json=_vehicle_update_payload())

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['status']", body["status"], "registered")
    check_is("body['previous_device_id']", body["previous_device_id"], None)


def test_update_vehicle_config_no_change_when_same_device_and_driver(client, conn):
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-001", "driver_id": 55})

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=55),
    )

    check("resp.status_code", resp.status_code, 200)
    check("body['status']", resp.json()["status"], "no_change")


def test_update_vehicle_config_driver_updated_when_device_same_but_driver_differs(client, conn):
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-001", "driver_id": 55})

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=99),
    )

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['status']", body["status"], "driver_updated")
    check("body['previous_driver_id']", body["previous_driver_id"], 55)
    check("body['driver_id']", body["driver_id"], 99)


def test_update_vehicle_config_migrates_when_device_changes(client, conn):
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-002", "driver_id": 55})
    conn.execute = AsyncMock(return_value="UPDATE 3")

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=55),
    )

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['status']", body["status"], "migrated")
    check("body['previous_device_id']", body["previous_device_id"], "KTC-002")
    check("body['migrated_trip_logs']", body["migrated_trip_logs"], 3)


def test_update_vehicle_config_migrate_result_parse_failure_defaults_to_zero(client, conn):
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-002", "driver_id": 55})
    conn.execute = AsyncMock(return_value="SOMETHING-WEIRD")

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=55),
    )

    check("resp.status_code", resp.status_code, 200)
    check("body['migrated_trip_logs']", resp.json()["migrated_trip_logs"], 0)


def test_update_vehicle_config_invalid_new_device_id_returns_422(client):
    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="BAD"),
    )

    check("resp.status_code (invalid new_device_id)", resp.status_code, 422)


def test_update_vehicle_config_invalid_old_device_id_returns_422(client):
    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(old_device_id="BAD"),
    )

    check("resp.status_code (invalid old_device_id)", resp.status_code, 422)


def test_update_vehicle_config_old_device_id_empty_string_treated_as_none(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(old_device_id=""),
    )

    check("resp.status_code (empty old_device_id)", resp.status_code, 200)


def test_update_vehicle_config_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db exploded"))

    resp = client.put("/api/v1/config/vehicle", json=_vehicle_update_payload())

    check("resp.status_code (db error)", resp.status_code, 500)


def test_update_vehicle_config_old_device_id_mismatch_is_noop_safety_check(client, conn):
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-999", "driver_id": 1})

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(
            new_device_id="KTC-001",
            old_device_id="KTC-888",
            driver_id=1,
        ),
    )

    check("resp.status_code (mismatch noop)", resp.status_code, 200)
    body = resp.json()
    check("body['status']", body["status"], "migrated")
    check("body['previous_device_id']", body["previous_device_id"], "KTC-999")


def test_update_vehicle_config_httpexception_from_inner_block_is_reraised(client, conn, monkeypatch):
    from fastapi import HTTPException as _HTTPException

    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-002", "driver_id": 1})
    conn.execute = AsyncMock(side_effect=_HTTPException(status_code=409, detail="mid-flow conflict"))

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=1),
    )

    check("resp.status_code (reraise 409)", resp.status_code, 409)
    check("resp.json()['detail']", resp.json()["detail"], "mid-flow conflict")


# =================================================================
# GET /api/v1/config/scoring/current
# =================================================================

def test_get_current_scoring_config_returns_active_config(client, pool):
    pool.fetchrow = AsyncMock(return_value={
        "id": 1,
        "config_name": "FDD v1.4 Default",
        "score_base": 100.0,
        "harsh_brake_deduct": 5.0,
        "harsh_accel_deduct": 3.0,
        "harsh_corner_deduct": 3.0,
        "speeding_deduct": 10.0,
        "idling_deduct": 2.0,
        "bump_deduct": 4.0,
        "harsh_brake_g": 0.4,
        "harsh_accel_g": 0.4,
        "harsh_corner_g": 0.4,
        "speeding_kmh_over": 20.0,
        "idle_min_threshold": 5.0,
        "max_deduct_per_trip": 50.0,
        "is_active": True,
        "effective_date": None,
        "synced_from_odoo_at": None,
    })

    resp = client.get("/api/v1/config/scoring/current")

    check("resp.status_code", resp.status_code, 200)
    body = resp.json()
    check("body['config_name']", body["config_name"], "FDD v1.4 Default")
    check_approx("body['score_base']", body["score_base"], 100.0)


def test_get_current_scoring_config_rounds_float_values(client, pool):
    pool.fetchrow = AsyncMock(return_value={
        "id": 1,
        "config_name": "Test",
        "score_base": 100.123456,
        "harsh_brake_deduct": 5.0,
        "harsh_accel_deduct": 3.0,
        "harsh_corner_deduct": 3.0,
        "speeding_deduct": 10.0,
        "idling_deduct": 2.0,
        "bump_deduct": 4.0,
        "harsh_brake_g": 0.4,
        "harsh_accel_g": 0.4,
        "harsh_corner_g": 0.4,
        "speeding_kmh_over": 20.0,
        "idle_min_threshold": 5.0,
        "max_deduct_per_trip": 50.0,
        "is_active": True,
        "effective_date": None,
        "synced_from_odoo_at": None,
    })

    resp = client.get("/api/v1/config/scoring/current")

    check("resp.status_code", resp.status_code, 200)
    check_approx("body['score_base'] (rounded)", resp.json()["score_base"], 100.1235, abs_tol=1e-4)


def test_get_current_scoring_config_no_active_config_returns_404(client, pool):
    pool.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/config/scoring/current")

    check("resp.status_code (no active config)", resp.status_code, 404)


def test_get_current_scoring_config_db_error_returns_500(client, pool):
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.get("/api/v1/config/scoring/current")

    check("resp.status_code (db error)", resp.status_code, 500)


# =================================================================
# POST /api/v1/config/scoring
# =================================================================

def test_push_scoring_config_success(client, conn):
    conn.fetchrow = AsyncMock(return_value={
        "id": 5,
        "config_name": "New Policy Q1",
        "score_base": 100.0,
        "harsh_brake_deduct": 5.0,
        "harsh_accel_deduct": 3.0,
        "harsh_corner_deduct": 3.0,
        "speeding_deduct": 10.0,
        "idling_deduct": 2.0,
        "bump_deduct": 4.0,
        "harsh_brake_g": 0.4,
        "harsh_accel_g": 0.4,
        "harsh_corner_g": 0.4,
        "speeding_kmh_over": 20.0,
        "idle_min_threshold": 5.0,
        "max_deduct_per_trip": 50.0,
        "is_active": True,
        "effective_date": None,
        "synced_from_odoo_at": None,
    })

    resp = client.post("/api/v1/config/scoring", json={"config_name": "New Policy Q1"})

    check("resp.status_code", resp.status_code, 201)
    body = resp.json()
    check("body['status']", body["status"], "success")
    print(f"  🔎 {'message contains config name':<28} -> actual={body['message']!r}")
    assert "New Policy Q1" in body["message"]
    check("body['config']['config_name']", body["config"]["config_name"], "New Policy Q1")


def test_push_scoring_config_uses_defaults_when_only_name_given(client, conn):
    conn.fetchrow = AsyncMock(return_value={
        "id": 1, "config_name": "Minimal", "score_base": 100.0,
        "harsh_brake_deduct": 5.0, "harsh_accel_deduct": 3.0,
        "harsh_corner_deduct": 3.0, "speeding_deduct": 10.0,
        "idling_deduct": 2.0, "bump_deduct": 4.0,
        "harsh_brake_g": 0.4, "harsh_accel_g": 0.4, "harsh_corner_g": 0.4,
        "speeding_kmh_over": 20.0, "idle_min_threshold": 5.0,
        "max_deduct_per_trip": 50.0, "is_active": True,
        "effective_date": None, "synced_from_odoo_at": None,
    })

    resp = client.post("/api/v1/config/scoring", json={"config_name": "Minimal"})

    check("resp.status_code", resp.status_code, 201)

    call_args = conn.fetchrow.await_args.args
    check("call_args[1] (config_name)", call_args[1], "Minimal")
    check_approx("call_args[2] (score_base default)", call_args[2], 100.0)


def test_push_scoring_config_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("insert failed"))

    resp = client.post("/api/v1/config/scoring", json={"config_name": "Broken"})

    check("resp.status_code (db error)", resp.status_code, 500)


def test_push_scoring_config_missing_config_name_returns_422(client):
    resp = client.post("/api/v1/config/scoring", json={})

    check("resp.status_code (missing config_name)", resp.status_code, 422)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))