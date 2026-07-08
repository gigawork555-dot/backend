# app/services/mqtt_subscriber.py

"""
MQTT Subscriber Service

Responsibilities:
- Connect to MQTT broker (EMQX)
- Subscribe to telemetry topic
- Verify HMAC signature (optional)
- Parse and validate payload
- Lookup vehicle_id from device binding
- Run server-side harsh-event detection (FDD §10.4/§12.3, config-driven)
- Store the ENRICHED record in telemetry_raw
- Trigger downstream processing (trip manager)

FDD v1.4 Compliant

FIXES (vs previous version):
  [BUG-1] on_message: asyncio.create_task() ใน paho thread → RuntimeError
          → แก้เป็น asyncio.run_coroutine_threadsafe() + บันทึก _loop ตอน startup

  [BUG-2] loop_forever() บน executor บล็อก asyncio ทั้งหมด ทำให้ retry loop พัง
          → แก้เป็น loop_start() / loop_stop() + asyncio.sleep() แทน

  [BUG-3] hmac.new() ไม่มีใน Python stdlib
          → แก้เป็น hmac.new() (ถูกต้อง)

  [FIX #1] Event detection ordering
          เดิม: store_telemetry() insert ค่า event ที่ ESP32 ส่งมาเอง
                (client-decided) แล้ว ep_process_event() คำนวณ event
                จาก server-side config อีกที แต่ผลลัพธ์ "enriched" นั้น
                ถูกทิ้ง (แค่ log) ไม่เคยเขียนกลับ DB เลย
                → harsh event ที่ server ตรวจสอบเองไม่เคยถูกบันทึกจริง
                  ขัดกับ FDD §12.3 ที่ต้องให้ server (ไม่ใช่ client)
                  เป็นผู้ตัดสิน event เพราะ Admin ปรับ threshold ผ่าน
                  config ได้แบบ real-time

          แก้ไข: สลับลำดับ — เรียก ep_process_event() ก่อน แล้ว merge
                 ผลลัพธ์ (event, event_severity) เข้า payload ก่อนค่อย
                 เรียก store_telemetry() ครั้งเดียว ด้วย payload ที่
                 ถูก enrich แล้ว

  [FIX #2] Config key mismatch
          เดิม: _DEFAULT_EVENT_CONFIG เป็น dict คงที่ในไฟล์นี้ ใช้ชื่อ
                key (threshold_brake_g, threshold_accel_g,
                threshold_corner_g) ที่ "ไม่ตรง" กับชื่อ key ที่
                event_processor.py อ่านจริง (threshold_harsh_brake,
                threshold_harsh_accel, threshold_harsh_corner) ทำให้
                ค่า config ที่ตั้งใจจะส่งเข้าไปไม่มีผลอะไรเลย —
                event_processor จะ fallback ไปใช้ default ของตัวเอง
                เสมอ ซึ่งตอนนั้นยังไม่ตรงกับ FDD §12.3 ด้วย
                (-0.4 / 0.3 / 0.5 แทนที่จะเป็น -0.4 / 0.4 / 0.4)

          แก้ไข: ลบ _DEFAULT_EVENT_CONFIG ทิ้ง แล้วดึง active scoring
                 config จริงจาก DB (scoring_config_cache) ทุกครั้ง ผ่าน
                 get_event_detection_config() ซึ่ง map ชื่อ column DB
                 → key ที่ event_processor.py อ่านโดยตรง (ชื่อเดียวกัน
                 กับที่ trip_manager.get_active_scoring_config() ใช้
                 สำหรับ score_calculator ด้วย เพื่อไม่ให้ config สอง
                 จุดในระบบ drift ออกจากกันอีก)

  [FIX #5 — this revision] ts ไม่ propagate ไป trip_manager
          เดิม: store_telemetry() normalize payload["ts"] (รองรับ
                None/string/int/float, ms→s, sanity pre-2020 fallback)
                แต่ผลลัพธ์เก็บไว้แค่ใน local variable `ts_epoch`
                ภายในฟังก์ชันเท่านั้น ไม่เคยเขียนกลับเข้า payload
                → เวลาที่ trip_manager.handle_telemetry() อ่าน
                  payload["ts"] เพื่อคำนวณ trip_start/trip_end มันยัง
                  เห็น ts ดิบที่บอร์ดส่งมา (ก่อนแปลง) เสมอ
                → กรณีบอร์ดส่ง ts แบบ millis()/1000 (เช่นตอนเพิ่งบูต
                  ยังไม่ sync เวลาเป็น epoch จริง) ค่านั้น "ดูเหมือน
                  ปี 1970" ไปตกอยู่ที่ trip_manager ตรงๆ ทำให้
                  trip_logs.trip_start กลายเป็นปี 1970 แทนที่จะเป็น
                  เวลา server ปัจจุบันตามที่ store_telemetry() ตั้งใจ
                  จะ fallback ให้

          แก้ไข: แยก logic normalize ts ออกมาเป็นฟังก์ชันกลาง
                 `_normalize_ts_epoch()` (pure function, idempotent)
                 ใช้ร่วมกันทั้งใน handle_telemetry() และ
                 store_telemetry() — handle_telemetry() คำนวณ
                 `fixed_ts` ครั้งเดียวแล้วเขียนกลับเข้า
                 `payload_to_store["ts"]` ก่อนส่งต่อทั้งไปยัง
                 store_telemetry() และ trip_manager.handle_telemetry()
                 ทำให้ทั้งสองฝั่งเห็นเวลาที่ normalize แล้วตรงกันเสมอ
"""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import paho.mqtt.client as mqtt

from app.config import settings
from app.database import get_db_pool
from app.services.trip_manager import handle_telemetry as trip_handle_telemetry
from app.services.event_processor import process_event as ep_process_event
from app.services.event_processor import BUMP_THRESHOLD_G

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Globals
# ──────────────────────────────────────────────────────────────

mqtt_client: Optional[mqtt.Client] = None
connected: bool = False

# [FIX-1] เก็บ reference ของ asyncio event loop ที่ FastAPI ใช้งาน
# ต้องบันทึกตอน mqtt_subscriber_task() เริ่ม (ใน async context)
# เพื่อให้ on_message() ซึ่งรันใน paho thread ใช้ run_coroutine_threadsafe() ได้
_loop: Optional[asyncio.AbstractEventLoop] = None


# ──────────────────────────────────────────────────────────────
# HMAC Verification (optional)
# ──────────────────────────────────────────────────────────────

def verify_hmac(payload_str: str, signature: str) -> bool:
    """
    Verify HMAC-SHA256 signature from ESP32.
    Returns True if HMAC_SECRET is not configured (feature disabled).
    """
    if not settings.HMAC_SECRET:
        return True

    try:
        expected = hmac.new(
            settings.HMAC_SECRET.encode(),
            payload_str.encode(),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(signature, expected)  # timing-safe compare

    except Exception as e:
        logger.warning(f"HMAC verification error: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# Lookup vehicle_id from device binding
# ──────────────────────────────────────────────────────────────

async def lookup_vehicle_id(
    pool: asyncpg.Pool,
    device_id: str,
) -> Optional[int]:
    """
    Lookup vehicle_id from devices table.
    Returns None if device is not yet bound to a vehicle.
    """
    try:
        vehicle_id = await pool.fetchval(
            "SELECT vehicle_id FROM devices WHERE id = $1 AND active = TRUE",
            device_id,
        )
        return vehicle_id

    except Exception as e:
        logger.warning(f"Error looking up vehicle for device {device_id}: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# [FIX #2] Event detection config — ดึงจาก DB จริง, key ตรงกับ
# event_processor.py เสมอ (single source of truth)
# ──────────────────────────────────────────────────────────────

# FDD v1.4 §12.3 defaults — ใช้เฉพาะตอนไม่มี active config ใน DB เลย
_FALLBACK_EVENT_CONFIG: dict = {
    "threshold_harsh_brake":   -0.40,   # FDD §10.4: ax < -0.4G
    "threshold_harsh_accel":    0.40,   # FDD §10.4: ax > +0.4G
    "threshold_harsh_corner":   0.40,   # FDD §10.4: |ay| > 0.4G
    "threshold_bump":           BUMP_THRESHOLD_G,  # FDD §10.4: fixed 3G, not config-driven
    "threshold_speed_kmh":     20.0,    # FDD §12.3: speeding_kmh_over default
    "threshold_idle_min":       5.0,    # FDD §12.3: idle_min_threshold default
}


async def get_event_detection_config(pool: asyncpg.Pool) -> dict:
    """
    ดึง active scoring config จาก scoring_config_cache แล้ว map ชื่อ
    column DB → key ที่ event_processor._detect_*() อ่านจริง

    [FIX #2] ก่อนหน้านี้ค่า config เหล่านี้ถูก hardcode แยกไว้ในไฟล์นี้
    ด้วยชื่อ key คนละชุดกับ event_processor.py ทำให้ config ไม่มีผลจริง
    ตอนนี้ดึงจาก DB ตรงๆ และ map ชื่อ key ให้ตรงกันแบบ explicit
    """

    try:
        row = await pool.fetchrow(
            """
            SELECT
                harsh_brake_g, harsh_accel_g, harsh_corner_g,
                speeding_kmh_over, idle_min_threshold
            FROM scoring_config_cache
            WHERE is_active = TRUE
            LIMIT 1
            """
        )
    except Exception as e:
        logger.warning(f"[MQTT] Failed to load scoring config: {e} — using FDD defaults")
        row = None

    if not row:
        return dict(_FALLBACK_EVENT_CONFIG)

    # DB stores harsh_brake_g as a positive magnitude (FDD default 0.40);
    # event_processor's brake threshold convention is negative (ax < -0.4G)
    harsh_brake_g = row["harsh_brake_g"]

    return {
        "threshold_harsh_brake":
            -abs(float(harsh_brake_g)) if harsh_brake_g is not None else -0.40,
        "threshold_harsh_accel":
            float(row["harsh_accel_g"]) if row["harsh_accel_g"] is not None else 0.40,
        "threshold_harsh_corner":
            float(row["harsh_corner_g"]) if row["harsh_corner_g"] is not None else 0.40,
        # FDD §10.4: bump threshold is a fixed constant, not Admin-configurable
        "threshold_bump":
            BUMP_THRESHOLD_G,
        "threshold_speed_kmh":
            float(row["speeding_kmh_over"]) if row["speeding_kmh_over"] is not None else 20.0,
        "threshold_idle_min":
            float(row["idle_min_threshold"]) if row["idle_min_threshold"] is not None else 5.0,
    }


# ──────────────────────────────────────────────────────────────
# [FIX #5] ts normalization — แยกออกมาเป็นฟังก์ชันกลาง (pure function)
#
# เดิม logic นี้อยู่ในตัว store_telemetry() ล้วนๆ และเก็บผลลัพธ์ไว้แค่
# ใน local variable ทำให้ trip_manager ที่อ่าน payload["ts"] โดยตรง
# (ไม่ผ่าน store_telemetry) ไม่เคยได้ค่าที่ normalize แล้วเลย
# ──────────────────────────────────────────────────────────────

def _normalize_ts_epoch(raw_ts) -> float:
    """
    Normalize ts ให้เป็น epoch seconds (float) เสมอ

    - รองรับ None / string / int / float
    - แปลง milliseconds → seconds ถ้าค่าใหญ่เกิน 1e11
    - ถ้าค่าดูเหมือนก่อนปี 2020 (บอร์ดยังไม่ sync เวลา) → ใช้เวลา server แทน

    Pure function — เรียกซ้ำได้อย่างปลอดภัย (idempotent) เพราะค่าที่
    normalize แล้วจะอยู่ในช่วง [1577836800, 1e11] เสมอ ไม่มีทาง
    ถูกตีความว่าเป็น milliseconds หรือ pre-2020 อีกในการเรียกครั้งถัดไป
    """
    if raw_ts is None:
        return datetime.now(timezone.utc).timestamp()

    if isinstance(raw_ts, str):
        try:
            ts_epoch = float(raw_ts)
        except ValueError:
            return datetime.now(timezone.utc).timestamp()
    else:
        ts_epoch = float(raw_ts)

    # ถ้า ts ใหญ่เกิน 1e11 แสดงว่าเป็น milliseconds → หาร 1000
    if ts_epoch > 1e11:
        ts_epoch = ts_epoch / 1000.0
        logger.debug(f"[MQTT] ts converted from ms to seconds: {ts_epoch}")

    # ตรวจ sanity: ถ้า ts ยังเป็น before 2020 → ใช้เวลาปัจจุบันแทน
    if ts_epoch < 1577836800:  # 2020-01-01 00:00:00 UTC
        logger.warning(
            f"[MQTT] ts={ts_epoch} ดูเหมือน GPS ยังไม่ sync เวลา "
            f"→ ใช้ server time แทน"
        )
        ts_epoch = datetime.now(timezone.utc).timestamp()

    return ts_epoch


# ──────────────────────────────────────────────────────────────
# Store Telemetry into telemetry_raw
# ──────────────────────────────────────────────────────────────

async def store_telemetry(
    pool: asyncpg.Pool,
    device_id: str,
    vehicle_id: Optional[int],  # ยังคง signature เดิม เพื่อไม่ให้ handle_telemetry() พัง
    payload: dict,
) -> int:
    """
    Insert raw telemetry record into TimescaleDB hypertable.
    Returns the new record ID.

    [FIX #1] `payload` ที่รับเข้ามาตอนนี้คือ payload ที่ผ่าน
    ep_process_event() มาแล้ว (enriched) — ค่า event/event_severity
    ที่ถูก insert คือค่าที่ server ตรวจสอบเอง ไม่ใช่ค่าดิบจาก ESP32

    [FIX #5] ts normalization ตอนนี้อยู่ใน _normalize_ts_epoch()
    (ฟังก์ชันกลาง ใช้ร่วมกับ handle_telemetry()) — ที่นี่ยังคงเรียกใช้
    ได้อย่างปลอดภัยแม้ payload["ts"] จะถูก normalize มาก่อนแล้วจาก
    handle_telemetry() ก็ตาม (idempotent) เผื่อกรณีมีที่อื่นเรียก
    store_telemetry() ตรงๆ โดยไม่ผ่าน handle_telemetry()

    หมายเหตุ schema: telemetry_raw ไม่มีคอลัมน์ vehicle_id และ created_at
    vehicle_id ถูก lookup แยก แต่ไม่ได้ store ใน raw table
    (join ผ่าน devices.vehicle_id ตอน query แทน)
    """
    # ── Normalize timestamp (ใช้ฟังก์ชันกลาง — FIX #5) ───────────
    ts_epoch = _normalize_ts_epoch(payload.get("ts"))

    # ── Normalize ignition ──────────────────────────────────────
    raw_ignition = payload.get("ignition")
    if isinstance(raw_ignition, int):
        ignition = bool(raw_ignition)
    elif raw_ignition is None:
        ignition = True   # default: ถ้าไม่ส่งมา assume ignition on
    else:
        ignition = raw_ignition

    # ── Normalize altitude ──────────────────────────────────────
    altitude = payload.get("altitude") or payload.get("alt")

    try:
        telemetry_id = await pool.fetchval(
            """
            INSERT INTO telemetry_raw (
                device_id, ts,
                lat, lon, speed, heading, altitude, hdop,
                rpm, throttle, engine_load, coolant_temp, fuel_level,
                maf_airflow,
                ax, ay, az, gx, gy, gz,
                event, event_severity, ignition,
                created_at
            )
            VALUES (
                $1,  to_timestamp($2),
                $3,  $4,  $5,  $6,  $7,  $8,
                $9,  $10, $11, $12, $13,
                $14,
                $15, $16, $17, $18, $19, $20,
                $21, $22, $23,
                NOW()
            )
            RETURNING id
            """,
            # ── $1-$2: Identity + Timestamp ──────────────────
            device_id,
            ts_epoch,
            # ── $3-$8: GPS ───────────────────────────────────
            payload.get("lat"),
            payload.get("lon"),
            payload.get("speed"),
            payload.get("heading"),
            altitude,
            payload.get("hdop"),
            # ── $9-$13: OBD-II ───────────────────────────────
            payload.get("rpm"),
            payload.get("throttle"),
            payload.get("engine_load"),
            payload.get("coolant_temp"),
            payload.get("fuel_level"),
            # ── $14: MAF ─────────────────────────────────────
            payload.get("maf_airflow") or payload.get("maf"),
            # ── $15-$20: IMU ─────────────────────────────────
            payload.get("ax"),
            payload.get("ay"),
            payload.get("az"),
            payload.get("gx"),
            payload.get("gy"),
            payload.get("gz"),
            # ── $21-$23: Events + Ignition ───────────────────
            # [FIX #1] ค่านี้ตอนนี้มาจาก server-side detection แล้ว
            payload.get("event") or None,
            payload.get("event_severity"),
            ignition,
        )

        return telemetry_id

    except Exception as e:
        logger.error(f"Error storing telemetry from {device_id}: {e}", exc_info=True)
        raise


# ──────────────────────────────────────────────────────────────
# Main telemetry processing pipeline
# ──────────────────────────────────────────────────────────────

async def handle_telemetry(
    pool: asyncpg.Pool,
    device_id: str,
    payload: dict,
) -> None:
    """
    Process one incoming MQTT telemetry message end-to-end.

    [FIX #1] Pipeline order changed:
    1. Lookup vehicle_id from device binding
    2. Load active event-detection config from DB (FIX #2 — correct keys)
    3. Run event_processor.process_event() → server-side harsh event
       detection (FDD §10.4/§12.3, config-driven, Admin can retune
       thresholds without redeploying firmware)
    4. [FIX #5] Normalize ts ONCE here via _normalize_ts_epoch()
    5. Merge the enriched event/event_severity + fixed ts into the payload
    6. Store the ENRICHED telemetry in telemetry_raw (single write)
    7. Pass to trip_manager.handle_telemetry (trip boundary detection),
       which now receives the SAME normalized ts that was stored —
       not the raw board ts — fixing trip_logs.trip_start landing on
       1970 when a board sends millis()/1000-style timestamps before
       its clock is synced.
    """
    try:
        # ── Step 1: Lookup vehicle ──────────────────────────────
        vehicle_id = await lookup_vehicle_id(pool, device_id)

        if vehicle_id is None:
            logger.warning(
                f"[TELEMETRY] Device '{device_id}' ไม่ได้ผูกกับรถคันไหน — "
                f"telemetry จะถูก store แต่ trip/event processing จะถูกข้าม "
                f"→ ให้เรียก PUT /api/v1/config/vehicle เพื่อผูก device กับรถก่อน"
            )
            try:
                await pool.execute(
                    """
                    INSERT INTO devices (id, active)
                    VALUES ($1, true)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    device_id,
                )
            except Exception as reg_err:
                logger.warning(f"[TELEMETRY] Auto-register device failed: {reg_err}")

        # ── Step 2: Load event-detection config (FIX #2) ────────
        event_config = await get_event_detection_config(pool)

        # ── Step 3: Server-side event detection (FIX #1) ────────
        # รันก่อน store เสมอ เพื่อให้ event ที่บันทึกเป็นค่าที่ server
        # ตรวจสอบเอง (config-driven) ไม่ใช่ค่าที่ ESP32 ส่งมาดิบๆ
        enriched = ep_process_event(
            payload={**payload, "device_id": device_id},
            config=event_config,
        )

        # ── Step 4: Normalize ts ครั้งเดียวตรงนี้ (FIX #5) ───────
        # เดิมค่านี้ถูก normalize แค่ภายใน store_telemetry() เป็น
        # local variable — ไม่เคยเขียนกลับเข้า payload เลย ทำให้
        # trip_manager ที่อ่าน payload["ts"] เห็นค่าดิบจากบอร์ดเสมอ
        fixed_ts = _normalize_ts_epoch(payload.get("ts"))

        # ── Step 5: Merge enriched event + fixed ts เข้า payload ─
        payload_to_store = {
            **payload,
            "ts": fixed_ts,  # [FIX #5] เขียน ts ที่ normalize แล้วกลับเข้า payload
            "event": enriched.get("event") or "",
            "event_severity": enriched.get("event_severity", 0.0),
        }

        # ── Step 6: Store ENRICHED telemetry (single write) ─────
        telemetry_id = await store_telemetry(
            pool, device_id, vehicle_id, payload_to_store
        )

        logger.info(
            f"[TELEMETRY STORED] id={telemetry_id} "
            f"device={device_id} bound_vehicle={vehicle_id} "
            f"lat={payload.get('lat')} lon={payload.get('lon')} "
            f"speed={payload.get('speed')} kmh "
            f"ignition={payload.get('ignition')} "
            f"event={payload_to_store['event'] or '-'}"
        )

        if enriched.get("event"):
            logger.info(
                f"[EVENT] device={device_id} "
                f"event={enriched['event']} "
                f"severity={enriched.get('event_severity', 0.0):.2f}"
            )

        # ── Step 7: Trip detection (requires vehicle binding) ───
        # [FIX #5] payload_to_store["ts"] ตอนนี้เป็น fixed_ts (epoch
        # จริงที่ normalize แล้ว) — trip_manager.handle_telemetry()
        # จะ datetime.fromtimestamp() ได้ค่าที่ถูกต้อง ไม่ใช่ปี 1970
        if vehicle_id is not None:
            payload_with_device = {**payload_to_store, "device_id": device_id}
            await trip_handle_telemetry(pool=pool, payload=payload_with_device)

    except Exception as e:
        logger.error(
            f"Error processing telemetry from '{device_id}': {e}",
            exc_info=True,
        )


# ──────────────────────────────────────────────────────────────
# Async wrapper (รันใน asyncio event loop ของ FastAPI)
# ──────────────────────────────────────────────────────────────

async def _process_message_async(device_id: str, payload: dict) -> None:
    """
    Async wrapper สำหรับ telemetry pipeline.
    รันผ่าน run_coroutine_threadsafe จาก on_message callback.
    """
    try:
        pool = await get_db_pool()
        await handle_telemetry(pool, device_id, payload)

    except Exception as e:
        logger.error(
            f"Async processing failed for device '{device_id}': {e}",
            exc_info=True,
        )


# ──────────────────────────────────────────────────────────────
# MQTT Callbacks (รันใน paho thread — ต้องไม่ใช้ asyncio โดยตรง)
# ──────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc, properties=None):
    """Called by paho when broker connection is established."""
    global connected

    if rc == 0:
        connected = True
        client.subscribe(settings.MQTT_TOPIC, qos=1)
        logger.info(
            f"[MQTT] Connected ✓  broker={settings.MQTT_HOST}:{settings.MQTT_PORT}"
        )
        logger.info(f"[MQTT] Subscribed → {settings.MQTT_TOPIC}  (QoS 1)")
    else:
        connected = False
        rc_messages = {
            1: "incorrect protocol version",
            2: "invalid client identifier",
            3: "server unavailable",
            4: "bad username or password",
            5: "not authorised",
        }
        reason = rc_messages.get(rc, f"unknown rc={rc}")
        logger.error(f"[MQTT] Connection refused: {reason}")


def on_disconnect(client, userdata, rc, properties=None):
    """Called by paho on disconnection."""
    global connected
    connected = False

    if rc == 0:
        logger.info("[MQTT] Disconnected gracefully")
    else:
        logger.warning(f"[MQTT] Unexpected disconnection rc={rc} — will retry")


def on_message(client, userdata, msg):
    """
    Called by paho thread when a message arrives.

    [FIX-1, kept] ห้ามใช้ asyncio.create_task() ที่นี่ เพราะรันอยู่ใน
    paho thread ซึ่งไม่มี running event loop ใน thread ของตัวเอง
    วิธีถูกต้อง: ใช้ asyncio.run_coroutine_threadsafe(coro, loop)
    """
    if _loop is None or not _loop.is_running():
        logger.warning("[MQTT] Event loop not ready — message dropped")
        return

    try:
        topic_parts = msg.topic.split("/")
        if len(topic_parts) >= 2:
            device_id = topic_parts[-2]   # ตำแหน่ง -2 = device_id
        else:
            device_id = topic_parts[-1]

        payload_str = msg.payload.decode("utf-8")
        payload     = json.loads(payload_str)

        logger.debug(
            f"[MQTT] RX topic={msg.topic} device={device_id} "
            f"size={len(msg.payload)}B"
        )

        # ── HMAC verification (optional) ─────────────────────
        signature = None
        if hasattr(msg, "properties") and msg.properties:
            signature = getattr(msg.properties, "hmac", None)

        if signature and not verify_hmac(payload_str, signature):
            logger.warning(f"[MQTT] HMAC failed — device={device_id} message dropped")
            return

        future = asyncio.run_coroutine_threadsafe(
            _process_message_async(device_id, payload),
            _loop,
        )

        def _on_done(fut):
            exc = fut.exception()
            if exc:
                logger.error(
                    f"[MQTT] Processing failed for device={device_id}: {exc}"
                )

        future.add_done_callback(_on_done)

    except json.JSONDecodeError as e:
        logger.error(f"[MQTT] Invalid JSON payload on {msg.topic}: {e}")
    except UnicodeDecodeError as e:
        logger.error(f"[MQTT] Cannot decode payload on {msg.topic}: {e}")
    except Exception as e:
        logger.error(f"[MQTT] on_message error: {e}", exc_info=True)


# ──────────────────────────────────────────────────────────────
# MQTT Subscriber Background Task
# ──────────────────────────────────────────────────────────────

async def mqtt_subscriber_task() -> None:
    """
    Background task: เชื่อมต่อ MQTT broker และรับ message ตลอดเวลา.
    ถูกเรียกจาก FastAPI lifespan startup.
    """
    global mqtt_client, connected, _loop

    _loop = asyncio.get_running_loop()

    retry_delay = 5

    while True:
        try:
            mqtt_client = mqtt.Client(
                client_id="fleet-telematics-backend",
                protocol=mqtt.MQTTv311,
                clean_session=True,
            )

            mqtt_client.on_connect    = on_connect
            mqtt_client.on_disconnect = on_disconnect
            mqtt_client.on_message    = on_message

            if settings.MQTT_USER and settings.MQTT_PASS:
                mqtt_client.username_pw_set(
                    settings.MQTT_USER,
                    settings.MQTT_PASS,
                )

            logger.info(
                f"[MQTT] Connecting to {settings.MQTT_HOST}:{settings.MQTT_PORT} ..."
            )

            mqtt_client.connect(
                settings.MQTT_HOST,
                settings.MQTT_PORT,
                keepalive=60,
            )

            mqtt_client.loop_start()

            while True:
                await asyncio.sleep(5)

                if not connected:
                    logger.warning("[MQTT] Connection lost — reconnecting ...")
                    break

                logger.debug(f"[MQTT] Heartbeat ✓  connected={connected}")

            mqtt_client.loop_stop()
            try:
                mqtt_client.disconnect()
            except Exception:
                pass

        except asyncio.CancelledError:
            logger.info("[MQTT] Subscriber task cancelled — shutting down")
            if mqtt_client:
                mqtt_client.loop_stop()
                try:
                    mqtt_client.disconnect()
                except Exception:
                    pass
            break

        except OSError as e:
            logger.error(
                f"[MQTT] Network error: {e}. Retry in {retry_delay}s ..."
            )
            connected = False

        except Exception as e:
            logger.error(
                f"[MQTT] Unexpected error: {e}. Retry in {retry_delay}s ...",
                exc_info=True,
            )
            connected = False

        try:
            await asyncio.sleep(retry_delay)
        except asyncio.CancelledError:
            break

        retry_delay = min(retry_delay * 2, 60)   # max 60s


# ──────────────────────────────────────────────────────────────
# Health Check
# ──────────────────────────────────────────────────────────────

def is_mqtt_connected() -> bool:
    """Return True if MQTT client is currently connected to broker."""
    return connected and mqtt_client is not None