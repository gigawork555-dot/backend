# tests/test_routes_config.py
"""
Coverage target: app/api/routes_config.py

ไฟล์นี้ยังไม่มี test มาก่อน — สร้างใหม่ครอบคลุมทุก endpoint ตาม
Device Configuration & Management module:

  - _validate_device_id_format()   : ฟังก์ชัน validation หลัก (defense-in-depth)
  - GET  /api/v1/devices           : list device ทั้งหมด
  - GET  /api/v1/config_device     : เช็คสถานะ binding ของ device เดี่ยว
  - POST /api/v1/config_device/register         : ผูก device เดี่ยว (409 conflict)
  - POST /api/v1/config_device/register/batch   : ผูกหลาย device (all-or-nothing)
  - PUT  /api/v1/config/vehicle     : bind/migrate device ให้รถ (4 status branches)
  - GET  /api/v1/config/scoring/current : active scoring config ปัจจุบัน
  - POST /api/v1/config/scoring     : Odoo push scoring config ใหม่

Testing strategy
-----------------
Endpoint เหล่านี้ทั้งหมดพึ่ง asyncpg.Pool / asyncpg.Connection จริง แต่
sandbox นี้ไม่มี PostgreSQL ให้เชื่อมต่อ ดังนั้นใช้แนวทาง:

1. Mock asyncpg.Pool ทั้งก้อน (fetchrow/fetch/execute/acquire)
2. pool.acquire() ต้องคืน async context manager ที่ yield connection mock
3. conn.transaction() ก็เป็น async context manager อีกชั้น (ไม่ yield ค่าพิเศษ)
4. Override FastAPI dependency get_db_pool ด้วย pool mock ผ่าน
   app.dependency_overrides — เพื่อยิง request ผ่าน TestClient จริง
   (ทดสอบ routing + Pydantic validation + business logic ครบเส้นทาง
   ไม่ใช่แค่เรียกฟังก์ชัน handler ตรงๆ)

หมายเหตุสำคัญ: routes_config.py มี 2 แบบการเข้าถึง DB ปนกันในไฟล์เดียว
  (ก) ผ่าน pool ตรงๆ (pool.fetch, pool.fetchrow)  — ใช้ใน GET /devices,
      GET /config_device, GET/POST config/scoring
  (ข) ผ่าน pool.acquire() แล้ว conn.transaction()  — ใช้ใน POST register,
      POST register/batch, PUT config/vehicle
ดังนั้น mock pool ต้องรองรับทั้งสองแบบพร้อมกัน (ดู `pool` fixture ด้านล่าง)
"""

from __future__ import annotations

import os
import sys

# ── Env vars จำเป็นก่อน import app.config.Settings (ค่า dummy พอ) ──
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

# ── Path bootstrap (เหมือน test_score_calculator.py เดิมในโปรเจค) ──
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api import routes_config          # noqa: E402
from app.database import get_db_pool       # noqa: E402
from app.api.routes_config import (        # noqa: E402
    _validate_device_id_format,
)


# =================================================================
# Fixtures — Mock asyncpg Pool / Connection / Transaction
# =================================================================

def _make_tx_cm():
    """conn.transaction() -> async context manager, ไม่ yield ค่าพิเศษ"""
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    return tx_cm


def _make_conn(fetchrow_side_effect=None, fetchrow_return=None,
               execute_return="INSERT 0 1"):
    """
    สร้าง connection mock หนึ่งตัว
    - ถ้าระบุ fetchrow_side_effect (list) จะคืนค่าตามลำดับการเรียกแต่ละครั้ง
      (ใช้เมื่อ endpoint เรียก conn.fetchrow() มากกว่า 1 ครั้งด้วยผลลัพธ์ต่างกัน)
    - ถ้าไม่ระบุ side_effect จะใช้ fetchrow_return แทน (ค่าคงที่ทุกครั้ง)
    """
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
    """
    pool.acquire() -> async context manager ที่ yield `conn`
    นอกจากนี้ยัง set pool.fetch / pool.fetchrow / pool.execute เป็น
    AsyncMock เปล่าไว้ล่วงหน้า (endpoint บางตัวเรียก pool ตรงๆ ไม่ผ่าน acquire)
    เทสต์แต่ละอันจะ override ค่าคืนของ pool.fetch/fetchrow เองตามต้องการ
    """
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
    """Connection mock พื้นฐาน — เทสต์ override .fetchrow/.execute เองตามเคส"""
    return _make_conn()


@pytest.fixture
def pool(conn):
    """Pool mock ที่ผูกกับ conn fixture ด้านบน"""
    return _make_pool(conn)


@pytest.fixture
def client(pool):
    """
    FastAPI TestClient พร้อม dependency override
    ทุกเทสต์ที่ใช้ fixture นี้ยิง HTTP request ผ่าน routing จริง
    """
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
    assert _validate_device_id_format("KTC-001") == "KTC-001"


def test_validate_device_id_format_normalizes_lowercase_to_uppercase():
    assert _validate_device_id_format("ktc-001") == "KTC-001"


def test_validate_device_id_format_strips_whitespace():
    assert _validate_device_id_format("  KTC-001  ") == "KTC-001"


def test_validate_device_id_format_none_passthrough():
    # v is None -> คืน None ตรงๆ โดยไม่ raise (ไม่ validate)
    assert _validate_device_id_format(None) is None


def test_validate_device_id_format_rejects_numeric_only():
    # กรณีนี้คือ root cause ของบั๊กเดิมตามที่ comment ในโค้ดอธิบายไว้
    with pytest.raises(ValueError, match="KTC-XXX"):
        _validate_device_id_format("1")


def test_validate_device_id_format_rejects_wrong_digit_count():
    with pytest.raises(ValueError):
        _validate_device_id_format("KTC-01")  # 2 หลัก ไม่ใช่ 3


def test_validate_device_id_format_rejects_missing_prefix():
    with pytest.raises(ValueError):
        _validate_device_id_format("XYZ-001")


def test_validate_device_id_format_error_message_includes_field_name():
    with pytest.raises(ValueError, match="new_device_id"):
        _validate_device_id_format("bad", field_name="new_device_id")


# =================================================================
# GET /api/v1/devices
# =================================================================

def test_get_devices_returns_list(client, pool):
    pool.fetch = AsyncMock(return_value=[
        {"id": "KTC-001", "vehicle_id": 101, "active": True, "registered_at": None},
        {"id": "KTC-002", "vehicle_id": None, "active": True, "registered_at": None},
    ])

    resp = client.get("/api/v1/devices")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["devices"]) == 2


def test_get_devices_empty_list(client, pool):
    pool.fetch = AsyncMock(return_value=[])

    resp = client.get("/api/v1/devices")

    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "devices": []}


def test_get_devices_db_error_returns_500(client, pool):
    pool.fetch = AsyncMock(side_effect=RuntimeError("connection lost"))

    resp = client.get("/api/v1/devices")

    assert resp.status_code == 500


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

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_bound"] is True
    assert body["status"] == "active"


def test_get_device_config_found_but_unbound(client, pool):
    pool.fetchrow = AsyncMock(return_value={
        "device_id": "KTC-002",
        "vehicle_id": None,
        "active": False,
        "date_update_latest": None,
    })

    resp = client.get("/api/v1/config_device", params={"device_id": "KTC-002"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_bound"] is False
    assert body["status"] == "inactive"


def test_get_device_config_not_found_returns_404(client, pool):
    pool.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/config_device", params={"device_id": "KTC-999"})

    assert resp.status_code == 404


def test_get_device_config_invalid_format_returns_422(client):
    # device_id="1" ไม่ตรง pattern KTC-XXX -> ควรโดนดักตั้งแต่ query-param validation
    resp = client.get("/api/v1/config_device", params={"device_id": "1"})

    assert resp.status_code == 422


def test_get_device_config_db_error_returns_500(client, pool):
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.get("/api/v1/config_device", params={"device_id": "KTC-001"})

    assert resp.status_code == 500


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
    # ไม่มี conflict ใดๆ -> fetchrow คืน None ทุกครั้ง (ค่า default ของ fixture)
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "success"
    assert body["device_id"] == "KTC-001"
    assert body["vehicle_id"] == 101


def test_register_device_single_invalid_format_returns_422(client):
    resp = client.post(
        "/api/v1/config_device/register",
        json=_valid_register_payload(device_id="1"),
    )

    assert resp.status_code == 422


def test_register_device_conflict_exact_same_binding_returns_409(client, conn):
    # Step 1 check: exact binding ซ้ำเดิม -> ต้องคืน 409 ทันที
    conn.fetchrow = AsyncMock(return_value={"vehicle_id": 101})

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    assert resp.status_code == 409
    assert "already bound to vehicle" in resp.json()["detail"]


def test_register_device_conflict_bound_to_different_vehicle_returns_409(client, conn):
    # Step 1: ไม่มี exact binding (None) -> Step 2: มี binding กับรถอื่น
    conn.fetchrow = AsyncMock(side_effect=[
        None,                          # step 1: no exact match
        {"vehicle_id": 202},           # step 2: bound to a DIFFERENT vehicle
    ])

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    assert resp.status_code == 409
    assert "already bound to vehicle 202" in resp.json()["detail"]


def test_register_device_conflict_vehicle_already_has_other_device_returns_409(client, conn):
    # Step 1 & 2 ผ่าน (None) -> Step 3: vehicle มี device อื่นผูกอยู่แล้ว
    conn.fetchrow = AsyncMock(side_effect=[
        None,                              # step 1
        None,                              # step 2
        {"device_id": "KTC-999"},          # step 3: vehicle already has different device
    ])

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    assert resp.status_code == 409
    assert "already bound to device KTC-999" in resp.json()["detail"]


def test_register_device_all_checks_pass_then_db_insert_fails_returns_500(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)  # ผ่านทุก conflict check
    conn.execute = AsyncMock(side_effect=RuntimeError("insert failed"))

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    assert resp.status_code == 500


def test_register_device_single_pool_acquire_itself_fails_returns_500(client, pool):
    # จำลอง pool.acquire() ล้มเหลวตั้งแต่เปิด connection (ไม่ใช่ error ระหว่าง
    # query) — ครอบคลุม `except Exception` ชั้นนอกสุดของ endpoint นี้โดยตรง
    # แทนที่จะเป็น HTTPException ซึ่งดักด้วย `except HTTPException: raise` ไปแล้ว
    class _BrokenAcquire:
        def __call__(self):
            raise RuntimeError("pool exhausted")

    pool.acquire = _BrokenAcquire()

    resp = client.post("/api/v1/config_device/register", json=_valid_register_payload())

    assert resp.status_code == 500


# =================================================================
# POST /api/v1/config_device/register/batch
# =================================================================

def test_register_device_batch_success(client, conn):
    conn.fetchrow = AsyncMock(return_value=None)  # ไม่มี conflict เลยทั้ง batch

    payload = {
        "devices": [
            _valid_register_payload("KTC-001", 101),
            _valid_register_payload("KTC-002", 102),
        ]
    }

    resp = client.post("/api/v1/config_device/register/batch", json=payload)

    assert resp.status_code == 201
    body = resp.json()
    assert body["registered"] == 2
    assert len(body["results"]) == 2


def test_register_device_batch_empty_list_returns_400(client):
    resp = client.post("/api/v1/config_device/register/batch", json={"devices": []})

    assert resp.status_code == 400


def test_register_device_batch_conflict_rolls_back_entire_batch(client, conn):
    # device แรกผ่านฉลุย (fetchrow None x3) แต่ device ที่สอง conflict ทันที
    # (exact binding ซ้ำ) -> ทั้ง batch ต้อง fail (all-or-nothing), ไม่ใช่แค่ตัวที่ conflict
    conn.fetchrow = AsyncMock(side_effect=[
        None, None, None,              # device 1: ผ่านทั้ง 3 check
        {"vehicle_id": 999},           # device 2: exact binding conflict ทันที (check แรก)
    ])

    payload = {
        "devices": [
            _valid_register_payload("KTC-001", 101),
            _valid_register_payload("KTC-002", 102),
        ]
    }

    resp = client.post("/api/v1/config_device/register/batch", json=payload)

    assert resp.status_code == 409


def test_register_device_batch_pool_acquire_itself_fails_returns_500(client, pool):
    # เช่นเดียวกับ single-register: ครอบคลุม `except Exception` ชั้นนอกสุด
    # ของ batch endpoint เมื่อ pool.acquire() เองพัง (ไม่ใช่ error จาก query)
    class _BrokenAcquire:
        def __call__(self):
            raise RuntimeError("pool exhausted")

    pool.acquire = _BrokenAcquire()

    payload = {"devices": [_valid_register_payload("KTC-001", 101)]}

    resp = client.post("/api/v1/config_device/register/batch", json=payload)

    assert resp.status_code == 500


def test_register_device_batch_invalid_device_id_in_list_returns_422(client):
    # Pydantic validate ทุก item ตั้งแต่รับ request -> ผิดตัวเดียวก็ 422 ทั้ง batch
    payload = {
        "devices": [
            _valid_register_payload("KTC-001", 101),
            _valid_register_payload("BAD", 102),
        ]
    }

    resp = client.post("/api/v1/config_device/register/batch", json=payload)

    assert resp.status_code == 422


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
    # current binding query -> None (รถยังไม่มีบอร์ด)
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.put("/api/v1/config/vehicle", json=_vehicle_update_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "registered"
    assert body["previous_device_id"] is None


def test_update_vehicle_config_no_change_when_same_device_and_driver(client, conn):
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-001", "driver_id": 55})

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=55),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "no_change"


def test_update_vehicle_config_driver_updated_when_device_same_but_driver_differs(client, conn):
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-001", "driver_id": 55})

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=99),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "driver_updated"
    assert body["previous_driver_id"] == 55
    assert body["driver_id"] == 99


def test_update_vehicle_config_migrates_when_device_changes(client, conn):
    # current binding: มีบอร์ดเก่า KTC-OLD ผูกกับ vehicle นี้อยู่
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-002", "driver_id": 55})
    # migrate_result string ต้องลงท้ายด้วยตัวเลขจำนวนแถวที่ถูก update (asyncpg format)
    conn.execute = AsyncMock(return_value="UPDATE 3")

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=55),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "migrated"
    assert body["previous_device_id"] == "KTC-002"
    assert body["migrated_trip_logs"] == 3


def test_update_vehicle_config_migrate_result_parse_failure_defaults_to_zero(client, conn):
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-002", "driver_id": 55})
    # execute คืนค่าที่ parse "แถวสุดท้าย" เป็น int ไม่ได้ -> ต้อง fallback เป็น 0 ไม่ raise
    conn.execute = AsyncMock(return_value="SOMETHING-WEIRD")

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=55),
    )

    assert resp.status_code == 200
    assert resp.json()["migrated_trip_logs"] == 0


def test_update_vehicle_config_invalid_new_device_id_returns_422(client):
    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="BAD"),
    )

    assert resp.status_code == 422


def test_update_vehicle_config_invalid_old_device_id_returns_422(client):
    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(old_device_id="BAD"),
    )

    assert resp.status_code == 422


def test_update_vehicle_config_old_device_id_empty_string_treated_as_none(client, conn):
    # old_device_id="" -> validator คืน None แทนที่จะ raise (ไม่ validate ค่าว่าง)
    conn.fetchrow = AsyncMock(return_value=None)

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(old_device_id=""),
    )

    assert resp.status_code == 200


def test_update_vehicle_config_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db exploded"))

    resp = client.put("/api/v1/config/vehicle", json=_vehicle_update_payload())

    assert resp.status_code == 500


def test_update_vehicle_config_old_device_id_mismatch_is_noop_safety_check(client, conn):
    # โค้ดปัจจุบันมี safety-check `if old_device_id mismatch: pass` ซึ่งเป็น
    # no-op โดยตั้งใจ (ยังไม่ implement การ reject จริง) — เทสต์นี้ยืนยันว่า
    # ต่อให้ Odoo ส่ง old_device_id มาไม่ตรงกับที่ DB บันทึกจริง endpoint ก็ยัง
    # ทำงานต่อได้ปกติ ไม่ throw error ใดๆ (สถานะปัจจุบันของโค้ด ไม่ใช่
    # behavior ที่ "ถูกต้องที่สุด" แต่ทดสอบเพื่อ lock พฤติกรรมปัจจุบันไว้ —
    # ถ้าในอนาคตมีการ implement การ reject จริง เทสต์นี้จะช่วยจับความเปลี่ยนแปลง)
    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-999", "driver_id": 1})

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(
            new_device_id="KTC-001",
            old_device_id="KTC-888",  # ไม่ตรงกับ KTC-999 ที่ DB มีจริง
            driver_id=1,
        ),
    )

    # ไม่ raise/reject — เดินหน้า migrate ตามปกติ (บอร์ดเดิมจาก DB คือ KTC-999)
    assert resp.status_code == 200
    assert resp.json()["status"] == "migrated"
    assert resp.json()["previous_device_id"] == "KTC-999"


def test_update_vehicle_config_httpexception_from_inner_block_is_reraised(client, conn, monkeypatch):
    # ครอบคลุม `except HTTPException: raise` (บรรทัด 647-648) — ต้องจำลอง
    # ให้เกิด HTTPException ขึ้นจริงระหว่าง flow ปกติของ endpoint นี้
    # วิธีที่ตรงและไม่ปะปนกับ mock อื่น: ให้ conn.execute() (ตอน migrate
    # บอร์ดเก่าออก) โยน HTTPException ออกมาโดยตรง จำลองว่ามี validation
    # อื่นในเลเยอร์ล่างที่ตัดสินใจ reject กลางทาง
    from fastapi import HTTPException as _HTTPException

    conn.fetchrow = AsyncMock(return_value={"device_id": "KTC-002", "driver_id": 1})
    conn.execute = AsyncMock(side_effect=_HTTPException(status_code=409, detail="mid-flow conflict"))

    resp = client.put(
        "/api/v1/config/vehicle",
        json=_vehicle_update_payload(new_device_id="KTC-001", driver_id=1),
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "mid-flow conflict"


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

    assert resp.status_code == 200
    body = resp.json()
    assert body["config_name"] == "FDD v1.4 Default"
    assert body["score_base"] == 100.0


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

    assert resp.status_code == 200
    # ต้องปัดเป็นทศนิยม 4 ตำแหน่งตาม dict-comprehension ในโค้ด
    assert resp.json()["score_base"] == 100.1235


def test_get_current_scoring_config_no_active_config_returns_404(client, pool):
    pool.fetchrow = AsyncMock(return_value=None)

    resp = client.get("/api/v1/config/scoring/current")

    assert resp.status_code == 404


def test_get_current_scoring_config_db_error_returns_500(client, pool):
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    resp = client.get("/api/v1/config/scoring/current")

    assert resp.status_code == 500


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

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "success"
    assert "New Policy Q1" in body["message"]
    assert body["config"]["config_name"] == "New Policy Q1"


def test_push_scoring_config_uses_defaults_when_only_name_given(client, conn):
    # ตรวจว่า Pydantic default values (score_base=100.0 ฯลฯ) ถูกส่งเข้า
    # conn.execute() จริง โดย inspect ค่าที่ conn.fetchrow ได้รับ
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

    assert resp.status_code == 201

    # ตรวจ SQL params ที่ถูกส่งเข้า fetchrow จริง (arg หลัง SQL string)
    call_args = conn.fetchrow.await_args.args
    # args[0] = SQL string, args[1] = config_name, args[2] = score_base, ...
    assert call_args[1] == "Minimal"
    assert call_args[2] == 100.0  # score_base default


def test_push_scoring_config_db_error_returns_500(client, conn):
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("insert failed"))

    resp = client.post("/api/v1/config/scoring", json={"config_name": "Broken"})

    assert resp.status_code == 500


def test_push_scoring_config_missing_config_name_returns_422(client):
    # config_name เป็น required field ใน ScoringConfigRequest
    resp = client.post("/api/v1/config/scoring", json={})

    assert resp.status_code == 422


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))
