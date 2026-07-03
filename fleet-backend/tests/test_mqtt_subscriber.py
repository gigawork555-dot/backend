# tests/test_mqtt_subscriber.py
"""
Coverage target (FDD §14.2): mqtt_subscriber.py >= 80%

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

# ── Path bootstrap (same pattern as test_score_calculator.py) ──────
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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
    assert verify_hmac("any payload", "any signature") is True


def test_verify_hmac_returns_true_for_correct_signature(monkeypatch):
    monkeypatch.setattr(mqtt_subscriber.settings, "HMAC_SECRET", "topsecret")
    payload_str = '{"device_id":"KTC-001"}'
    expected_sig = hmac_stdlib.new(
        b"topsecret", payload_str.encode(), hashlib.sha256
    ).hexdigest()

    assert verify_hmac(payload_str, expected_sig) is True


def test_verify_hmac_returns_false_for_wrong_signature(monkeypatch):
    monkeypatch.setattr(mqtt_subscriber.settings, "HMAC_SECRET", "topsecret")
    payload_str = '{"device_id":"KTC-001"}'
    assert verify_hmac(payload_str, "0" * 64) is False


def test_verify_hmac_returns_false_on_internal_exception(monkeypatch):
    monkeypatch.setattr(mqtt_subscriber.settings, "HMAC_SECRET", "topsecret")

    # payload_str.encode() will raise AttributeError since None has no
    # .encode() -> exercises the except branch, returns False
    result = verify_hmac(None, "deadbeef")  # type: ignore[arg-type]
    assert result is False


# =================================================================
# lookup_vehicle_id()
# =================================================================

async def test_lookup_vehicle_id_returns_value_on_success():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)

    result = await lookup_vehicle_id(pool, "KTC-001")

    assert result == 101
    pool.fetchval.assert_awaited_once()


async def test_lookup_vehicle_id_returns_none_when_unbound():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)

    result = await lookup_vehicle_id(pool, "KTC-999")

    assert result is None


async def test_lookup_vehicle_id_returns_none_on_db_exception():
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("connection lost"))

    result = await lookup_vehicle_id(pool, "KTC-001")

    assert result is None


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

    assert config["threshold_harsh_brake"] == -0.5   # sign flipped
    assert config["threshold_harsh_accel"] == 0.45
    assert config["threshold_harsh_corner"] == 0.35
    assert config["threshold_speed_kmh"] == 25.0
    assert config["threshold_idle_min"] == 6.0
    assert config["threshold_bump"] == mqtt_subscriber.BUMP_THRESHOLD_G


async def test_get_event_detection_config_falls_back_when_no_active_row():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    config = await get_event_detection_config(pool)

    assert config == _FALLBACK_EVENT_CONFIG


async def test_get_event_detection_config_falls_back_on_db_exception():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    config = await get_event_detection_config(pool)

    assert config == _FALLBACK_EVENT_CONFIG


async def test_get_event_detection_config_handles_null_columns_in_row():
    # row exists but individual columns are NULL -> per-field defaults
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={
        "harsh_brake_g": None,
        "harsh_accel_g": None,
        "harsh_corner_g": None,
        "speeding_kmh_over": None,
        "idle_min_threshold": None,
    })

    config = await get_event_detection_config(pool)

    assert config["threshold_harsh_brake"] == -0.40
    assert config["threshold_harsh_accel"] == 0.40
    assert config["threshold_harsh_corner"] == 0.40
    assert config["threshold_speed_kmh"] == 20.0
    assert config["threshold_idle_min"] == 5.0


# =================================================================
# store_telemetry() — timestamp normalization
# =================================================================

def _get_positional_args(async_mock):
    """Extract the positional args passed to an AsyncMock call
    (index 0 is always the SQL string)."""
    call = async_mock.await_args
    return call.args


async def test_store_telemetry_epoch_seconds_used_as_is():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    ts_seconds = 1750000000  # a valid post-2020 epoch, in seconds
    await store_telemetry(pool, "KTC-001", 101, {"ts": ts_seconds, "ignition": True})

    args = _get_positional_args(pool.fetchval)
    ts_epoch_sent = args[2]  # $1=device_id, $2=ts_epoch
    assert ts_epoch_sent == pytest.approx(float(ts_seconds))


async def test_store_telemetry_epoch_milliseconds_converted_to_seconds():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    ts_ms = 1750000000000  # milliseconds -> > 1e11
    await store_telemetry(pool, "KTC-001", 101, {"ts": ts_ms, "ignition": True})

    args = _get_positional_args(pool.fetchval)
    ts_epoch_sent = args[2]
    assert ts_epoch_sent == pytest.approx(ts_ms / 1000.0)


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
    assert ts_epoch_sent == pytest.approx(fixed_now.timestamp())


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
    assert args[2] == pytest.approx(fixed_now.timestamp())


async def test_store_telemetry_pre_2020_ts_sanity_fallback_to_now(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    fixed_now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(mqtt_subscriber, "datetime", _FixedDateTime)

    # GPS not yet synced -> looks like 1970 epoch
    await store_telemetry(pool, "KTC-001", 101, {"ts": 1000, "ignition": True})

    args = _get_positional_args(pool.fetchval)
    assert args[2] == pytest.approx(fixed_now.timestamp())


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
        (None, True),  # default when omitted entirely from payload branch
    ],
)
async def test_store_telemetry_ignition_normalization(raw_ignition, expected):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    payload = {"ts": 1750000000, "ignition": raw_ignition}
    await store_telemetry(pool, "KTC-001", 101, payload)

    args = _get_positional_args(pool.fetchval)
    ignition_sent = args[-1]  # last positional arg is ignition ($23)
    assert ignition_sent is expected


async def test_store_telemetry_ignition_non_int_non_none_passthrough_unchanged():
    # anything that's not int/bool and not None falls into the `else`
    # branch and is stored completely as-is (no coercion at all)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    await store_telemetry(pool, "KTC-001", 101, {"ts": 1750000000, "ignition": "weird-value"})

    args = _get_positional_args(pool.fetchval)
    assert args[-1] == "weird-value"


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
    altitude_sent = args[6]  # $1 dev,$2 ts,$3 lat,$4 lon,$5 speed,$6 heading,$7 altitude
    # NOTE: index recount — see explicit column order below
    # columns: device_id($1) ts($2) lat($3) lon($4) speed($5) heading($6) altitude($7)
    altitude_sent = args[7]
    assert altitude_sent == 310.5


async def test_store_telemetry_event_empty_string_normalized_to_none():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)

    await store_telemetry(
        pool, "KTC-001", 101,
        {"ts": 1750000000, "ignition": True, "event": ""},
    )

    args = _get_positional_args(pool.fetchval)
    event_sent = args[-3]  # $21 event, $22 event_severity, $23 ignition
    assert event_sent is None


async def test_store_telemetry_raises_and_logs_on_db_error():
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("insert failed"))

    with pytest.raises(RuntimeError):
        await store_telemetry(pool, "KTC-001", 101, {"ts": 1750000000, "ignition": True})


# =================================================================
# handle_telemetry() — full pipeline
# =================================================================

async def test_handle_telemetry_bound_vehicle_calls_trip_manager(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)   # lookup_vehicle_id -> bound
    pool.fetchrow = AsyncMock(return_value=None)  # no active scoring config
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

    store_mock.assert_awaited_once()
    trip_mock.assert_awaited_once()

    # payload passed to trip_manager must include device_id
    _, kwargs = trip_mock.await_args
    assert kwargs["payload"]["device_id"] == "KTC-001"


async def test_handle_telemetry_unbound_vehicle_auto_registers_and_skips_trip(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)  # lookup_vehicle_id -> unbound
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    trip_mock = AsyncMock()
    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", trip_mock)

    store_mock = AsyncMock(return_value=555)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    payload = {"ts": 1750000000, "ignition": True}

    await handle_telemetry(pool, "KTC-UNBOUND", payload)

    # auto-register attempted
    pool.execute.assert_awaited_once()
    # store still happens even without a bound vehicle
    store_mock.assert_awaited_once()
    # trip manager must NOT be invoked when vehicle is unbound
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

    # must not raise even though pool.execute() raised internally
    await handle_telemetry(pool, "KTC-BROKEN", {"ts": 1750000000, "ignition": True})

    store_mock.assert_awaited_once()


async def test_handle_telemetry_merges_enriched_event_into_stored_payload(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=101)
    pool.fetchrow = AsyncMock(return_value=None)  # fallback event config
    pool.execute = AsyncMock()

    monkeypatch.setattr(mqtt_subscriber, "trip_handle_telemetry", AsyncMock())

    store_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(mqtt_subscriber, "store_telemetry", store_mock)

    # ax below -0.4G harsh-brake threshold -> event_processor should
    # classify this as harsh_brake regardless of what the client sent
    payload = {
        "ts": 1750000000, "ignition": True,
        "ax": -0.9, "ay": 0.0, "az": 1.0, "speed": 40.0,
        "event": "totally_wrong_client_value",
    }

    await handle_telemetry(pool, "KTC-001", payload)

    store_mock.assert_awaited_once()
    _, call_args, _ = store_mock.mock_calls[0]
    stored_payload = call_args[3]  # store_telemetry(pool, device_id, vehicle_id, payload)
    assert stored_payload["event"] == "harsh_brake"


async def test_handle_telemetry_swallows_unexpected_exception(monkeypatch):
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("boom"))

    # must not raise out of handle_telemetry — errors are logged only
    await handle_telemetry(pool, "KTC-ERR", {"ts": 1750000000, "ignition": True})


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


async def test_process_message_async_swallows_exception(monkeypatch):
    monkeypatch.setattr(
        mqtt_subscriber, "get_db_pool", AsyncMock(side_effect=RuntimeError("pool not ready"))
    )
    # must not raise
    await mqtt_subscriber._process_message_async("KTC-001", {"ts": 1750000000})


# =================================================================
# on_connect() / on_disconnect()
# =================================================================

def test_on_connect_success_subscribes_topic():
    client = MagicMock()
    on_connect(client, None, {}, 0)

    assert mqtt_subscriber.connected is True
    client.subscribe.assert_called_once_with(mqtt_subscriber.settings.MQTT_TOPIC, qos=1)


def test_on_connect_failure_sets_disconnected():
    client = MagicMock()
    on_connect(client, None, {}, 5)  # rc=5 -> not authorised

    assert mqtt_subscriber.connected is False
    client.subscribe.assert_not_called()


def test_on_connect_unknown_rc_code_handled_gracefully():
    client = MagicMock()
    on_connect(client, None, {}, 99)
    assert mqtt_subscriber.connected is False


def test_on_disconnect_graceful():
    mqtt_subscriber.connected = True
    on_disconnect(None, None, 0)
    assert mqtt_subscriber.connected is False


def test_on_disconnect_unexpected():
    mqtt_subscriber.connected = True
    on_disconnect(None, None, 7)
    assert mqtt_subscriber.connected is False


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

    # should not raise
    on_message(None, None, msg)


def test_on_message_dispatches_valid_payload(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        # a loop object whose is_running() reports True, without
        # actually needing a live running loop for this sync-only test
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

        assert "coro" in captured
        captured["coro"].close()  # avoid "coroutine was never awaited" warning
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

    on_message(None, None, msg)  # must not raise


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
    msg.topic = "singlesegment"  # no "/" -> topic_parts has len 1
    msg.payload = json.dumps({"ts": 1750000000}).encode()
    msg.properties = None

    on_message(None, None, msg)

    assert "coro" in captured
    captured["coro"].close()


def test_on_message_done_callback_logs_when_future_raised(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)

    captured = {}

    def fake_dispatch(coro, loop):
        coro.close()  # avoid "never awaited" warning, we don't need to run it
        fut = MagicMock()
        captured["future"] = fut
        return fut

    monkeypatch.setattr(mqtt_subscriber.asyncio, "run_coroutine_threadsafe", fake_dispatch)

    msg = _make_msg("kotchasaan/fleet/KTC-001/telemetry", {"ts": 1750000000})
    on_message(None, None, msg)

    fut = captured["future"]
    # capture the _on_done callback that on_message registered
    done_callback = fut.add_done_callback.call_args.args[0]

    # branch 1: future raised an exception -> logs error
    fut.exception.return_value = RuntimeError("processing exploded")
    done_callback(fut)  # must not raise

    # branch 2: future completed cleanly -> no error log
    fut.exception.return_value = None
    done_callback(fut)


def test_on_message_unicode_decode_error_handled(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)

    msg = MagicMock()
    msg.topic = "kotchasaan/fleet/KTC-001/telemetry"
    msg.payload = b"\xff\xfe\x00\xff"  # invalid utf-8
    msg.properties = None

    on_message(None, None, msg)  # must not raise


def test_on_message_generic_exception_handled(monkeypatch):
    fake_loop = MagicMock()
    fake_loop.is_running.return_value = True
    monkeypatch.setattr(mqtt_subscriber, "_loop", fake_loop)

    msg = MagicMock()
    # msg.topic is not a string -> .split("/") raises AttributeError,
    # falling into the generic `except Exception` branch
    msg.topic = None
    msg.payload = b'{"ts": 1}'
    msg.properties = None

    on_message(None, None, msg)  # must not raise


# =================================================================
# mqtt_subscriber_task() — background connect/retry/cancel loop
# =================================================================

async def test_mqtt_subscriber_task_connects_detects_loss_and_cancels_cleanly(monkeypatch):
    """
    Exercises the happy-path connect, the "connection lost -> retry"
    branch, and the CancelledError shutdown branch, using a fully
    mocked paho Client (no real broker) and a monkeypatched
    asyncio.sleep so the test doesn't actually wait on the real 5s
    heartbeat / retry-delay intervals.
    """
    fake_client = MagicMock()
    monkeypatch.setattr(
        mqtt_subscriber.mqtt, "Client", MagicMock(return_value=fake_client)
    )

    real_sleep = asyncio.sleep
    sleep_calls = {"n": 0}

    async def fast_sleep(seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] == 1:
            # simulate the connection dropping right after the first
            # heartbeat check inside the inner keep-alive loop
            mqtt_subscriber.connected = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(mqtt_subscriber.mqtt_subscriber_task())

    # let a few iterations run (connect -> heartbeat -> lost -> retry
    # -> reconnect) before tearing down
    for _ in range(5):
        await real_sleep(0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

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
    assert is_mqtt_connected() is False


def test_is_mqtt_connected_true_when_connected_and_client_present():
    mqtt_subscriber.connected = True
    mqtt_subscriber.mqtt_client = MagicMock()
    assert is_mqtt_connected() is True


def test_is_mqtt_connected_false_when_disconnected():
    mqtt_subscriber.connected = False
    mqtt_subscriber.mqtt_client = MagicMock()
    assert is_mqtt_connected() is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"] + sys.argv[1:]))
