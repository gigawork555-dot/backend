# tests/test_mqtt_subscriber.py
"""
Coverage target (FDD §14.2): mqtt_subscriber.py >= 80%

[แก้ไข] ทุก assert ถูกแทนที่ด้วย check()/check_approx()/check_range()
จาก conftest.py เพื่อ print ค่า actual/expected จริงก่อนเช็ค
รันด้วย `-v -s`:

    docker compose run --rm backend pytest tests/test_mqtt_subscriber.py -v -s

Covers:
- verify_hmac(): correct signature, wrong signature, HMAC disabled
  (empty secret), and malformed-signature exception path
- store_telemetry(): timestamp normalization (seconds / milliseconds /
  invalid string / pre-2020 sanity fallback), ignition normalization
  (int 0/1, None default, bool passthrough), altitude "alt" fallback
- get_event_detection_config(): active-row mapping, no-active-row
  fallback, and DB-exception fallback
- lookup_vehicle_id(): success and DB-exception-returns-None paths
- handle_telemetry(): full pipeline (vehicle bound + unbound/auto
  -register branch), enriched event merged into the stored payload,
  and trip_manager invoked only when vehicle_id is resolved
- on_connect() / on_disconnect() success and failure branches
- on_message(): event-loop-not-ready guard, valid message dispatch,
  and invalid-JSON graceful handling
- is_mqtt_connected()

Uses pytest-asyncio + unittest.mock (AsyncMock/MagicMock) — no real
MQTT broker or database connection required.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_stdlib
import json
import os
import sys
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

# ── Path bootstrap ──────────────────────────────────────────────
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TEST_DIR = os.path.dirname(__file__)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)

from conftest import check, check_is, check_approx  # noqa: E402

import app.services.mqtt_subscriber as mqtt_subscriber  # noqa: E402
from app.services.mqtt_subscriber import (  # noqa: E402
    verify_hmac,
    lookup_vehicle_id,
    get_event_detection_config,
    store_telemetry,
    handle_telemetry,
    on_connect,
    on_disconnect,
    on_message,
    is_mqtt_connected,
    _FALLBACK_EVENT_CONFIG,
    _normalize_ts_epoch,
)


@pytest.fixture(autouse=True)
def _reset_mqtt_globals():
    mqtt_subscriber.connected = False
    mqtt_subscriber.mqtt_client = None
    yield
    mqtt_subscriber.connected = False
    mqtt_subscriber.mqtt_client = None


# =================================================================
# verify_hmac()
# =================================================================

def test_verify_hmac_returns_true_when_secret_disabled(monkeypatch):
    monkeypatch.setattr(mqtt_subscriber.settings, "HMAC_SECRET", "")
    result = verify_hmac("any payload", "any signature")
    check_is("verify_hmac(secret disabled)", result, True)


def test_verify_hmac_returns_true_for_correct_signature(monkeypatch):
    monkeypatch.setattr(mqtt_subscriber.settings, "HMAC_SECRET", "topsecret")
    payload_str = '{"device_id":"KTC-001"}'
    expected_sig = hmac_stdlib.new(
        b"topsecret", payload_str.encode(), hashlib.sha256
    ).hexdigest()

    result = verify_hmac(payload_str, expected_sig)
    check_is("verify_hmac(correct sig)", result, True)


def test_verify_hmac_returns_false_for_wrong_signature(monkeypatch):
    monkeypatch.setattr(mqtt_subscriber.settings, "HMAC_SECRET", "topsecret")
    payload_str = '{"device_id":"KTC-001"}'
    result = verify_hmac(payload_str, "0" * 64)
    check_is("verify_hmac(wrong sig)", result, False)


def test_verify_hmac_returns_false_on_internal_exception(monkeypatch):
    monkeypatch.setattr(mqtt_subscriber.settings, "HMAC_SECRET", "topsecret")
    result = verify_hmac(None, "deadbeef")  # type: ignore[arg-type]
    check_is("verify_hmac(None payload)", result, False)


# =================================================================
# lookup_vehicle_id()
# =================================================================

async def test_lookup_vehicle_id_returns_value_on_success():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)

    result = await lookup_vehicle_id(pool, "KTC-001")

    check("lookup_vehicle_id result", result, 101)
    pool.fetchval.assert_awaited_once()


async def test_lookup_vehicle_id_returns_none_when_unbound():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)

    result = await lookup_vehicle_id(pool, "KTC-999")

    check("lookup_vehicle_id result (unbound)", result, None)


async def test_lookup_vehicle_id_returns_none_on_db_exception():
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("connection lost"))

    result = await lookup_vehicle_id(pool, "KTC-001")

    check("lookup_vehicle_id result (db error)", result, None)


# =================================================================
# get_event_detection_config()
# =================================================================

async def test_get_event_detection_config_maps_active_row():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={
        "harsh_brake_g": 0.5,
        "harsh_accel_g": 0.45,
        "harsh_corner_g": 0.35,
        "speeding_kmh_over": 25.0,
        "idle_min_threshold": 6.0,
    })

    config = await get_event_detection_config(pool)

    check("config['threshold_harsh_brake']", config["threshold_harsh_brake"], -0.5)
    check("config['threshold_harsh_accel']", config["threshold_harsh_accel"], 0.45)
    check("config['threshold_harsh_corner']", config["threshold_harsh_corner"], 0.35)
    check("config['threshold_speed_kmh']", config["threshold_speed_kmh"], 25.0)
    check("config['threshold_idle_min']", config["threshold_idle_min"], 6.0)
    check("config['threshold_bump']", config["threshold_bump"], mqtt_subscriber.BUMP_THRESHOLD_G)


async def test_get_event_detection_config_falls_back_when_no_active_row():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    config = await get_event_detection_config(pool)

    check("config (no active row)", config, _FALLBACK_EVENT_CONFIG)


async def test_get_event_detection_config_falls_back_on_db_exception():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    config = await get_event_detection_config(pool)

    check("config (db exception)", config, _FALLBACK_EVENT_CONFIG)


async def test_get_event_detection_config_handles_null_columns_in_row():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={
        "harsh_brake_g": None,
        "harsh_accel_g": None,
        "harsh_corner_g": None,
        "speeding_kmh_over": None,
        "idle_min_threshold": None,
    })

    config = await get_event_detection_config(pool)

    check("config['threshold_harsh_brake']", config["threshold_harsh_brake"], -0.40)
    check("config['threshold_harsh_accel']", config["threshold_harsh_accel"], 0.40)
    check("config['threshold_harsh_corner']", config["threshold_harsh_corner"], 0.40)
    check("config['threshold_speed_kmh']", config["threshold_speed_kmh"], 20.0)
    check("config['threshold_idle_min']", config["threshold_idle_min"], 5.0)


# =================================================================
# _normalize_ts_epoch()
# =================================================================

def test_normalize_ts_epoch_none_falls_back_to_now(monkeypatch):
    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(mqtt_subscriber, "datetime", _FixedDateTime)

    result = _normalize_ts_epoch(None)
    check_approx("_normalize_ts_epoch(None)", result, fixed_now.timestamp())


def test_normalize_ts_epoch_seconds_passthrough():
    result = _normalize_ts_epoch(1750000000)
    check_approx("_normalize_ts_epoch(1750000000)", result, 1750000000.0)


def test_normalize_ts_epoch_string_seconds_parsed():
    result = _normalize_ts_epoch("1750000000")
    check_approx("_normalize_ts_epoch('1750000000')", result, 1750000000.0)


def test_normalize_ts_epoch_invalid_string_falls_back_to_now(monkeypatch):
    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(mqtt_subscriber, "datetime", _FixedDateTime)

    result = _normalize_ts_epoch("not-a-number")
    check_approx("_normalize_ts_epoch('not-a-number')", result, fixed_now.timestamp())


def test_normalize_ts_epoch_milliseconds_converted_to_seconds():
    ts_ms = 1750000000000
    result = _normalize_ts_epoch(ts_ms)
    check_approx("_normalize_ts_epoch(ms)", result, ts_ms / 1000.0)


def test_normalize_ts_epoch_pre_2020_falls_back_to_now(monkeypatch):
    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(mqtt_subscriber, "datetime", _FixedDateTime)

    result = _normalize_ts_epoch(11)
    check_approx("_normalize_ts_epoch(11, pre-2020)", result, fixed_now.timestamp())


def test_normalize_ts_epoch_is_idempotent():
    once = _normalize_ts_epoch(1750000000000)
    twice = _normalize_ts_epoch(once)
    check_approx("_normalize_ts_epoch(idempotent)", once, twice)


# =================================================================
# store_telemetry() — timestamp normalization
# =================================================================

def _get_positional_args(async_mock):
    call = async_mock.await_args
    return call.args


async def test_store_telemetry_epoch_seconds_used_as_is():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    ts_seconds = 1750000000
    await store_telemetry(pool, "KTC-001", 101, {"ts": ts_seconds, "ignition": True})

    args = _get_positional_args(pool.fetchval)
    ts_epoch_sent = args[2]
    check_approx("ts_epoch_sent (seconds)", ts_epoch_sent, float(ts_seconds))


async def test_store_telemetry_epoch_milliseconds_converted_to_seconds():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    ts_ms = 1750000000000
    await store_telemetry(pool, "KTC-001", 101, {"ts": ts_ms, "ignition": True})

    args = _get_positional_args(pool.fetchval)
    ts_epoch_sent = args[2]
    check_approx("ts_epoch_sent (ms->s)", ts_epoch_sent, ts_ms / 1000.0)


async def test_store_telemetry_invalid_string_ts_falls_back_to_now(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(mqtt_subscriber, "datetime", _FixedDateTime)

    await store_telemetry(pool, "KTC-001", 101, {"ts": "not-a-number", "ignition": True})

    args = _get_positional_args(pool.fetchval)
    ts_epoch_sent = args[2]
    check_approx("ts_epoch_sent (invalid string)", ts_epoch_sent, fixed_now.timestamp())


async def test_store_telemetry_missing_ts_falls_back_to_now(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(mqtt_subscriber, "datetime", _FixedDateTime)

    await store_telemetry(pool, "KTC-001", 101, {"ignition": True})

    args = _get_positional_args(pool.fetchval)
    check_approx("ts_epoch_sent (missing ts)", args[2], fixed_now.timestamp())


async def test_store_telemetry_pre_2020_ts_sanity_fallback_to_now(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(mqtt_subscriber, "datetime", _FixedDateTime)

    await store_telemetry(pool, "KTC-001", 101, {"ts": 1000, "ignition": True})

    args = _get_positional_args(pool.fetchval)
    check_approx("ts_epoch_sent (pre-2020)", args[2], fixed_now.timestamp())


# =================================================================
# store_telemetry() — ignition normalization
# =================================================================

@pytest.mark.parametrize(
    "raw_ignition, expected",
    [
        (1, True),
        (0, False),
        (True, True),
        (False, False),
        (None, True),
    ],
)
async def test_store_telemetry_ignition_normalization(raw_ignition, expected):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    payload = {"ts": 1750000000, "ignition": raw_ignition}
    await store_telemetry(pool, "KTC-001", 101, payload)

    args = _get_positional_args(pool.fetchval)
    ignition_sent = args[-1]
    check_is(f"ignition_sent (raw={raw_ignition!r})", ignition_sent, expected)


async def test_store_telemetry_ignition_non_int_non_none_passthrough_unchanged():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    await store_telemetry(pool, "KTC-001", 101, {"ts": 1750000000, "ignition": "weird-value"})

    args = _get_positional_args(pool.fetchval)
    check("ignition_sent (passthrough)", args[-1], "weird-value")


# =================================================================
# store_telemetry() — altitude fallback + event passthrough
# =================================================================

async def test_store_telemetry_uses_alt_key_when_altitude_missing():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    await store_telemetry(
        pool, "KTC-001", 101,
        {"ts": 1750000000, "ignition": True, "alt": 310.5},
    )

    args = _get_positional_args(pool.fetchval)
    # columns: device_id($1) ts($2) lat($3) lon($4) speed($5) heading($6) altitude($7)
    altitude_sent = args[7]
    check("altitude_sent (from 'alt' key)", altitude_sent, 310.5)


async def test_store_telemetry_event_empty_string_normalized_to_none():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    await store_telemetry(
        pool, "KTC-001", 101,
        {"ts": 1750000000, "ignition": True, "event": ""},
    )

    args = _get_positional_args(pool.fetchval)
    event_sent = args[-3]
    check("event_sent (empty string -> None)", event_sent, None)


async def test_store_telemetry_raises_and_logs_on_db_error():
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("insert failed"))

    with pytest.raises(RuntimeError):
        await store_telemetry(pool, "KTC-001", 101, {"ts": 1750000000, "ignition": True})
    print("  🔎 store_telemetry raised RuntimeError -> actual=True expected=True ✅")


# =================================================================
# handle_telemetry() — full pipeline
# =================================================================

async def test_handle_telemetry_bound_vehicle_calls_trip_manager(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    trip_mock = AsyncMock()
    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", trip_mock)

    store_mock = AsyncMock(return_value=555)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    payload = {
        "ts": 1750000000, "lat": 13.7, "lon": 100.5, "speed": 40.0,
        "ignition": True, "ax": 0.0, "ay": 0.0, "az": 1.0,
    }

    await handle_telemetry(pool, "KTC-001", payload)

    print(f"  🔎 store_mock await count        -> actual={store_mock.await_count} expected=1")
    store_mock.assert_awaited_once()
    print(f"  🔎 trip_mock await count         -> actual={trip_mock.await_count} expected=1")
    trip_mock.assert_awaited_once()

    _, kwargs = trip_mock.await_args
    check("kwargs['payload']['device_id']", kwargs["payload"]["device_id"], "KTC-001")


async def test_handle_telemetry_unbound_vehicle_auto_registers_and_skips_trip(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    trip_mock = AsyncMock()
    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", trip_mock)

    store_mock = AsyncMock(return_value=555)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    payload = {"ts": 1750000000, "ignition": True}

    await handle_telemetry(pool, "KTC-UNBOUND", payload)

    print(f"  🔎 pool.execute await count      -> actual={pool.execute.await_count} expected=1 (auto-register)")
    pool.execute.assert_awaited_once()
    print(f"  🔎 store_mock await count        -> actual={store_mock.await_count} expected=1")
    store_mock.assert_awaited_once()
    print(f"  🔎 trip_mock await count         -> actual={trip_mock.await_count} expected=0 (unbound)")
    trip_mock.assert_not_awaited()


async def test_handle_telemetry_auto_register_failure_is_swallowed(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock(side_effect=RuntimeError("insert conflict"))

    trip_mock = AsyncMock()
    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", trip_mock)
    store_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    await handle_telemetry(pool, "KTC-BROKEN", {"ts": 1750000000, "ignition": True})

    print(f"  🔎 store_mock await count        -> actual={store_mock.await_count} expected=1 (error swallowed)")
    store_mock.assert_awaited_once()


async def test_handle_telemetry_merges_enriched_event_into_stored_payload(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", AsyncMock())

    store_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    payload = {
        "ts": 1750000000, "ignition": True,
        "ax": -0.9, "ay": 0.0, "az": 1.0, "speed": 40.0,
        "event": "totally_wrong_client_value",
    }

    await handle_telemetry(pool, "KTC-001", payload)

    store_mock.assert_awaited_once()
    _, call_args, _ = store_mock.mock_calls[0]
    stored_payload = call_args[3]
    check("stored_payload['event'] (server-side detect)", stored_payload["event"], "harsh_brake")


async def test_handle_telemetry_propagates_fixed_ts_to_trip_manager(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    trip_mock = AsyncMock()
    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", trip_mock)

    store_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    raw_boot_ts = 11
    payload = {"ts": raw_boot_ts, "ignition": True}

    await handle_telemetry(pool, "KTC-002", payload)

    _, kwargs = trip_mock.await_args
    propagated_ts = kwargs["payload"]["ts"]

    print(f"  🔎 propagated_ts != raw_boot_ts  -> actual={propagated_ts!r} != {raw_boot_ts!r}")
    assert propagated_ts != raw_boot_ts
    print(f"  🔎 propagated_ts > 2020 epoch    -> actual={propagated_ts!r} expected > 1577836800")
    assert propagated_ts > 1577836800


async def test_handle_telemetry_propagates_normal_ts_unchanged_to_trip_manager(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    trip_mock = AsyncMock()
    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", trip_mock)

    store_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    normal_ts = 1750000000
    payload = {"ts": normal_ts, "ignition": True}

    await handle_telemetry(pool, "KTC-003", payload)

    _, kwargs = trip_mock.await_args
    check_approx("trip_manager ts", kwargs["payload"]["ts"], float(normal_ts))

    _, call_args, _ = store_mock.mock_calls[0]
    stored_payload = call_args[3]
    check_approx("store_telemetry ts", stored_payload["ts"], float(normal_ts))


async def test_handle_telemetry_propagates_millisecond_ts_converted_to_trip_manager(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    trip_mock = AsyncMock()
    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", trip_mock)

    store_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    ts_ms = 1750000000000
    payload = {"ts": ts_ms, "ignition": True}

    await handle_telemetry(pool, "KTC-004", payload)

    _, kwargs = trip_mock.await_args
    check_approx("trip_manager ts (ms->s)", kwargs["payload"]["ts"], ts_ms / 1000.0)


async def test_handle_telemetry_swallows_unexpected_exception(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("boom"))

    await handle_telemetry(pool, "KTC-ERR", {"ts": 1750000000, "ignition": True})
    print("  🔎 handle_telemetry did not raise -> actual=True expected=True ✅")


# =================================================================
# _process_message_async()
# =================================================================

async def test_process_message_async_success_path(monkeypatch):
    fake_pool = MagicMock()
    monkeypatch.setattr(mqtt_subscriber, "get_db_pool", AsyncMock(return_value=fake_pool))

    handle_mock = AsyncMock()
    monkeypatch.setattr(mqtt_subscriber, "handle_telemetry", handle_mock)

    await mqtt_subscriber._process_message_async("KTC-001", {"ts": 1750000000})

    handle_mock.assert_awaited_once_with(fake_pool, "KTC-001", {"ts": 1750000000})
    print("  🔎 handle_mock called with correct args -> ✅")


async def test_process_message_async_swallows_exception(monkeypatch):
    monkeypatch.setattr(
        mqtt_subscriber, "get_db_pool", AsyncMock(side_effect=RuntimeError("pool not ready"))
    )
    await mqtt_subscriber._process_message_async("KTC-001", {"ts": 1750000000})
    print("  🔎 _process_message_async did not raise -> actual=True expected=True ✅")


# =================================================================
# on_connect() / on_disconnect()
# =================================================================

def test_on_connect_success_subscribes_topic():
    client = MagicMock()
    on_connect(client, None, {}, 0)

    check("mqtt_subscriber.connected", mqtt_subscriber.connected, True)
    client.subscribe.assert_called_once_with(mqtt_subscriber.settings.MQTT_TOPIC, qos=1)


def test_on_connect_failure_sets_disconnected():
    client = MagicMock()
    on_connect(client, None, {}, 5)

    check("mqtt_subscriber.connected (rc=5)", mqtt_subscriber.connected, False)
    client.subscribe.assert_not_called()


def test_on_connect_unknown_rc_code_handled_gracefully():
    client = MagicMock()
    on_connect(client, None, {}, 99)
    check("mqtt_subscriber.connected (rc=99)", mqtt_subscriber.connected, False)


def test_on_disconnect_graceful():
    mqtt_subscriber.connected = True
    on_disconnect(None, None, 0)
    check("mqtt_subscriber.connected (rc=0)", mqtt_subscriber.connected, False)


def test_on_disconnect_unexpected():
    mqtt_subscriber.connected = True
    on_disconnect(None, None, 7)
    check("mqtt_subscriber.connected (rc=7)", mqtt_subscriber.connected, False)


# =================================================================
# on_message()
# =================================================================

def _make_msg(topic, payload_dict):
    msg = MagicMock()
    msg.topic = topic
    msg.payload = json.dumps(payload_dict).encode("utf-8")
    msg.properties = None
    return msg


def test_on_message_drops_when_event_loop_not_ready():
    mqtt_subscriber._loop = None
    msg = _make_msg("kotchasaan/fleet/KTC-001/telemetry", {"ts": 1750000000})

    on_message(None, None, msg)
    print("  🔎 on_message did not raise (loop None) -> actual=True expected=True ✅")


def test_on_message_dispatches_valid_payload(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        fake_loop = MagicMock()
        fake_loop.is_running.return_value = True

        captured = {}

        def fake_run_coroutine_threadsafe(coro, loop):
            captured["coro"] = coro
            fut = MagicMock()
            fut.exception.return_value = None
            return fut

        monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)
        monkeypatch.setattr(
            mqtt_subscriber.asyncio,
            "run_coroutine_threadsafe",
            fake_run_coroutine_threadsafe,
        )

        msg = _make_msg("kotchasaan/fleet/KTC-001/telemetry", {"ts": 1750000000, "ignition": True})
        on_message(None, None, msg)

        check("'coro' captured", "coro" in captured, True)
        captured["coro"].close()
    finally:
        loop.close()


def test_on_message_invalid_json_does_not_raise(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)

    msg = MagicMock()
    msg.topic = "kotchasaan/fleet/KTC-001/telemetry"
    msg.payload = b"{not valid json"
    msg.properties = None

    on_message(None, None, msg)
    print("  🔎 on_message did not raise (bad JSON) -> actual=True expected=True ✅")


def test_on_message_hmac_failure_drops_message(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)
    monkeypatch.setattr(mqtt_subscriber, "verify_hmac", lambda *_: False)

    dispatch_mock = MagicMock()
    monkeypatch.setattr(mqtt_subscriber.asyncio, "run_coroutine_threadsafe", dispatch_mock)

    msg = MagicMock()
    msg.topic = "kotchasaan/fleet/KTC-001/telemetry"
    msg.payload = json.dumps({"ts": 1750000000}).encode()

    props = MagicMock()
    props.hmac = "some-signature"
    msg.properties = props

    on_message(None, None, msg)

    print(f"  🔎 dispatch_mock call count      -> actual={dispatch_mock.call_count} expected=0 (dropped)")
    dispatch_mock.assert_not_called()


def test_on_message_single_segment_topic_uses_last_part_as_device_id(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)

    captured = {}

    def fake_dispatch(coro, loop):
        captured["coro"] = coro
        fut = MagicMock()
        fut.exception.return_value = None
        return fut

    monkeypatch.setattr(mqtt_subscriber.asyncio, "run_coroutine_threadsafe", fake_dispatch)

    msg = MagicMock()
    msg.topic = "singlesegment"
    msg.payload = json.dumps({"ts": 1750000000}).encode()
    msg.properties = None

    on_message(None, None, msg)

    check("'coro' captured (single segment topic)", "coro" in captured, True)
    captured["coro"].close()


def test_on_message_done_callback_logs_when_future_raised(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)

    captured = {}

    def fake_dispatch(coro, loop):
        coro.close()
        fut = MagicMock()
        captured["future"] = fut
        return fut

    monkeypatch.setattr(mqtt_subscriber.asyncio, "run_coroutine_threadsafe", fake_dispatch)

    msg = _make_msg("kotchasaan/fleet/KTC-001/telemetry", {"ts": 1750000000})
    on_message(None, None, msg)

    fut = captured["future"]
    done_callback = fut.add_done_callback.call_args.args[0]

    fut.exception.return_value = RuntimeError("processing exploded")
    done_callback(fut)
    print("  🔎 done_callback(error future) did not raise -> actual=True expected=True ✅")

    fut.exception.return_value = None
    done_callback(fut)
    print("  🔎 done_callback(clean future) did not raise -> actual=True expected=True ✅")


def test_on_message_unicode_decode_error_handled(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)

    msg = MagicMock()
    msg.topic = "kotchasaan/fleet/KTC-001/telemetry"
    msg.payload = b"\xff\xfe\x00\xff"
    msg.properties = None

    on_message(None, None, msg)
    print("  🔎 on_message did not raise (bad unicode) -> actual=True expected=True ✅")


def test_on_message_generic_exception_handled(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)

    msg = MagicMock()
    msg.topic = None
    msg.payload = b'{"ts": 1}'
    msg.properties = None

    on_message(None, None, msg)
    print("  🔎 on_message did not raise (topic=None) -> actual=True expected=True ✅")


# =================================================================
# mqtt_subscriber_task()
# =================================================================

async def test_mqtt_subscriber_task_connects_detects_loss_and_cancels_cleanly(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(
        mqtt_subscriber.mqtt, "Client", MagicMock(return_value=fake_client)
    )

    real_sleep = asyncio.sleep
    sleep_calls = {"n": 0}

    async def fast_sleep(seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] == 1:
            mqtt_subscriber.connected = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(mqtt_subscriber.mqtt_subscriber_task())

    for _ in range(5):
        await real_sleep(0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print(f"  🔎 fake_client.connect called    -> actual={fake_client.connect.called} expected=True")
    fake_client.connect.assert_called()
    fake_client.loop_start.assert_called()
    fake_client.loop_stop.assert_called()
    fake_client.disconnect.assert_called()


# =================================================================
# is_mqtt_connected()
# =================================================================

def test_is_mqtt_connected_false_when_no_client():
    mqtt_subscriber.connected = True
    mqtt_subscriber.mqtt_client = None
    result = is_mqtt_connected()
    check_is("is_mqtt_connected(no client)", result, False)


def test_is_mqtt_connected_true_when_connected_and_client_present():
    mqtt_subscriber.connected = True
    mqtt_subscriber.mqtt_client = MagicMock()
    result = is_mqtt_connected()
    check_is("is_mqtt_connected(connected+client)", result, True)


def test_is_mqtt_connected_false_when_disconnected():
    mqtt_subscriber.connected = False
    mqtt_subscriber.mqtt_client = MagicMock()
    result = is_mqtt_connected()
    check_is("is_mqtt_connected(disconnected)", result, False)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"] + sys.argv[1:]))