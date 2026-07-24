# mock_full.py
"""
Mock Hardware Stream — Full Version (ตรงกับ main.cpp ENABLE_MOCK_DATA)

จำลอง ESP32 10 บอร์ด วน round-robin ส่งข้อมูล MQTT telemetry ไม่จบ
อิงพฤติกรรมและ payload schema ให้ตรงกับ main.cpp:

- 10 device profile คงที่ (device_id, base_lat, base_lon, base_speed, event_type)
  ตรงกับ mockProfiles[] ใน main.cpp
- แต่ละบอร์ดมี ignition cycle ของตัวเอง (ON 60-300s, OFF 45-150s แบบสุ่ม)
  เหมือน updateMockIgnitionCycle() — เพื่อให้ backend trip_manager.py
  เห็น ignition True->False->True และสร้าง/ปิด trip ได้จริง
- ทุก 4 tick ของบอร์ดที่มี event_type จะ "ยิง" event 1 ครั้งเสมอ (ไม่สุ่ม)
  พร้อมค่า sensor ที่ผ่านเกณฑ์ FDD §10.4 ชัดเจน (เหมือน updateMockData())
- payload fields ตรงกับ buildPayloadFor() ใน main.cpp ทุกตัว:
    device_id, device_name, ts, lat, lon, speed, heading, alt, hdop,
    rpm, throttle, engine_load, coolant_temp, fuel_level, maf,
    ax, ay, az, gx, gy, gz, event, event_severity, ignition,
    temperature, humidity
- เซ็น payload ด้วย HMAC-SHA256 แบบเดียวกับ generate_signed_payload()
  เดิม (ตัด "}" ตัวท้ายออกแล้วต่อ ,"sig":"<hex>"} เข้าไป)

วิธีใช้:
    python mock_full.py

ปรับ MQTT_HOST / MQTT_PORT / HMAC_SECRET ให้ตรงกับ environment จริงก่อนรัน
(ค่า default ในไฟล์นี้ใช้ค่าเดียวกับ mock_hardware_stream.py เดิม)
"""

import json
import hmac
import hashlib
import time
import random
import asyncio
from dataclasses import dataclass, field
from typing import Optional

from paho.mqtt import client as mqtt_client

# =====================================================
# Connection Config — ปรับให้ตรงกับ environment จริง
# =====================================================
MQTT_HOST = "192.168.1.37"
MQTT_PORT = 1884
HMAC_SECRET = "fleet_hmac_secret_KTC001_2026"

# ทุกกี่วินาทีถึงจะ publish ข้อมูลของบอร์ดถัดไปในคิว (round-robin)
# main.cpp ใช้ MQTT_PUBLISH_INTERVAL_MS (นิยามใน config.h) — ในเวอร์ชัน
# mock นี้ใช้ค่าคงที่แทน ปรับได้ตามต้องการ
PUBLISH_INTERVAL_SECONDS = 3.0

# ยิง event ประจำโปรไฟล์ทุกกี่ tick ของบอร์ดนั้น (ตรงกับ main.cpp: tick % 4 == 0)
EVENT_EVERY_N_TICKS = 4

# ── Ignition cycle ranges (ตรงกับ main.cpp MOCK_IGNITION_*_SECONDS) ──
IGNITION_ON_MIN_SECONDS = 60
IGNITION_ON_MAX_SECONDS = 300
IGNITION_OFF_MIN_SECONDS = 45
IGNITION_OFF_MAX_SECONDS = 150


# =====================================================
# Device Profiles — ตรงกับ mockProfiles[] ใน main.cpp
# =====================================================
@dataclass
class MockDeviceProfile:
    device_id: str
    base_lat: float
    base_lon: float
    base_speed: float   # km/h ปกติของบอร์ดนี้
    event_type: str      # "" = ขับปกติไม่มี event เลย


MOCK_PROFILES: list[MockDeviceProfile] = [
    MockDeviceProfile("KTC-001", 13.7563, 100.5018, 42.0, "harsh_brake"),        # กรุงเทพฯ
    MockDeviceProfile("KTC-002", 18.7883,  98.9853, 58.0, "harsh_acceleration"),  # เชียงใหม่
    MockDeviceProfile("KTC-003", 13.9126, 100.6068, 33.0, "harsh_cornering"),    # ปทุมธานี
    MockDeviceProfile("KTC-004", 14.9799, 102.0977, 96.0, "speeding"),           # นครราชสีมา
    MockDeviceProfile("KTC-005",  7.8804,  98.3923,  0.0, "idling"),             # ภูเก็ต
    MockDeviceProfile("KTC-006", 16.4419, 102.8360, 25.0, "bump"),               # ขอนแก่น
    MockDeviceProfile("KTC-007", 12.9236, 100.8825, 61.0, ""),                   # ชลบุรี — ขับปกติ
    MockDeviceProfile("KTC-008",  9.1382,  99.3215, 47.0, "harsh_brake"),        # สุราษฎร์ธานี
    MockDeviceProfile("KTC-009", 17.4138, 102.7859, 88.0, "speeding"),           # อุดรธานี
    MockDeviceProfile("KTC-010",  6.9271,  99.6412,  0.0, "idling"),             # สงขลา
]


# =====================================================
# Per-device mutable state
# =====================================================
@dataclass
class TelemetryData:
    device_id: str = ""
    device_name: str = ""
    lat: float = 0.0
    lon: float = 0.0
    speed: float = 0.0
    heading: int = 0
    altitude: float = 0.0
    hdop: float = 1.0
    rpm: int = 0
    throttle: float = 0.0
    engine_load: float = 0.0
    coolant_temp: float = 0.0
    fuel_level: float = 0.0
    maf: float = 0.0
    ax: float = 0.0
    ay: float = 0.0
    az: float = 1.0
    gx: float = 0.0
    gy: float = 0.0
    gz: float = 0.0
    event: str = ""
    event_severity: float = 0.0
    ignition: bool = False
    temperature: float = 0.0
    humidity: float = 0.0
    ts: int = 0


@dataclass
class MockIgnitionState:
    ignition_on: bool = True
    state_started_ts: int = 0
    current_duration: int = 0
    initialized: bool = False


# state ต่อบอร์ด — index ตรงกับ MOCK_PROFILES
tick_count = [0] * len(MOCK_PROFILES)
ignition_state = [MockIgnitionState() for _ in MOCK_PROFILES]
mock_tele = [TelemetryData() for _ in MOCK_PROFILES]


def _random_state_duration(ignition_on: bool) -> int:
    if ignition_on:
        return random.randint(IGNITION_ON_MIN_SECONDS, IGNITION_ON_MAX_SECONDS)
    return random.randint(IGNITION_OFF_MIN_SECONDS, IGNITION_OFF_MAX_SECONDS)


def update_mock_ignition_cycle(idx: int, t: TelemetryData) -> bool:
    """
    อัปเดตสถานะ ignition ของบอร์ด idx ตาม t.ts ปัจจุบัน
    เทียบเท่า updateMockIgnitionCycle() ใน main.cpp
    คืน True ถ้าเพิ่งสลับสถานะรอบนี้
    """
    st = ignition_state[idx]

    if not st.initialized:
        st.ignition_on = random.randint(0, 99) < 70  # 70% เริ่มต้นวิ่งอยู่
        st.current_duration = _random_state_duration(st.ignition_on)
        phase_offset = random.randint(0, max(0, st.current_duration - 1))
        st.state_started_ts = t.ts - phase_offset
        st.initialized = True

    elapsed = t.ts - st.state_started_ts

    just_switched = False
    if elapsed >= st.current_duration:
        st.ignition_on = not st.ignition_on
        st.state_started_ts = t.ts
        st.current_duration = _random_state_duration(st.ignition_on)
        just_switched = True

    t.ignition = st.ignition_on

    if not st.ignition_on:
        # ดับเครื่อง: นิ่งสนิท ห้ามมี event ใดๆ (ตรงกับ main.cpp)
        t.speed = 0.0
        t.rpm = 0
        t.throttle = 0.0
        t.event = ""
        t.event_severity = 0.0
        t.ax, t.ay, t.az = 0.0, 0.0, 1.0
        t.gx, t.gy, t.gz = 0.0, 0.0, 0.0

    return just_switched


def update_mock_data(idx: int) -> TelemetryData:
    """
    สร้างข้อมูล telemetry ของบอร์ดลำดับ idx สำหรับ tick นี้
    เทียบเท่า updateMockData() ใน main.cpp
    """
    p = MOCK_PROFILES[idx]
    t = mock_tele[idx]

    tick_count[idx] += 1
    tick = tick_count[idx]

    t.device_id = p.device_id
    t.device_name = p.device_id
    t.ts = int(time.time()) + idx  # เวลาต่างกันทุกบอร์ด กันค่าซ้ำ

    # GPS: ขยับจากจุดฐานตาม tick เฉพาะของบอร์ดนั้น
    t.lat = p.base_lat + float((tick * 7 + idx * 3) % 97) * 0.00003
    t.lon = p.base_lon + float((tick * 11 + idx * 5) % 89) * 0.00003
    t.heading = int((tick * 13 + idx * 29) % 360)
    t.altitude = 50.0 + float((tick + idx) % 40) * 3.0
    t.hdop = 0.8 + float((tick + idx) % 15) * 0.1

    wobble = float((tick * (idx + 1)) % 21) - 10.0  # -10..+10

    t.speed = max(0.0, p.base_speed + wobble)
    t.rpm = int(900 + t.speed * 22 + (idx * 17))
    t.throttle = min(100.0, max(0.0, t.speed * 0.9 + wobble))
    t.engine_load = min(100.0, max(0.0, 35.0 + wobble * 1.2))
    t.coolant_temp = 82.0 + float((tick + idx) % 12)
    t.fuel_level = max(5.0, 90.0 - float((tick + idx * 4) % 85))
    t.maf = 3.0 + (t.speed * 0.12)

    # IMU พื้นฐาน (นิ่งใกล้ 0 / 1G) — จะถูก override ด้านล่างเมื่อ event เกิด
    t.ax = wobble * 0.01
    t.ay = wobble * 0.008
    t.az = 1.0 + (wobble * 0.003)
    t.gx = wobble * 0.05
    t.gy = wobble * 0.04
    t.gz = wobble * 0.03

    t.temperature = 27.0 + float((tick + idx) % 90) * 0.1
    t.humidity = 55.0 + float((tick + idx * 2) % 300) * 0.1

    # ── Event: ยิงชัดเจนทุก EVENT_EVERY_N_TICKS tick ──────────────
    has_profile_event = len(p.event_type) > 0
    fire_event = has_profile_event and (tick % EVENT_EVERY_N_TICKS == 0)

    t.event = ""
    t.event_severity = 0.0

    if fire_event:
        ev = p.event_type
        t.event = ev

        if ev == "harsh_brake":
            t.ax = -0.65             # ax < -0.4G ตาม FDD §10.4
            t.event_severity = 82.0
        elif ev == "harsh_acceleration":
            t.ax = 0.62               # ax > +0.4G
            t.event_severity = 78.0
        elif ev == "harsh_cornering":
            t.ay = 0.58               # |ay| > 0.4G
            t.event_severity = 74.0
        elif ev == "speeding":
            t.speed = p.base_speed + 25.0
            t.rpm = int(900 + t.speed * 22)
            t.event_severity = 90.0
        elif ev == "bump":
            t.az = 3.6                # az > +3G ตาม FDD §10.4
            t.event_severity = 70.0
        elif ev == "idling":
            t.speed = 0.0
            t.rpm = 800
            t.event_severity = 100.0
    elif has_profile_event and p.event_type == "idling":
        # บอร์ดสาย idling ให้จอดสนิทตลอด (ไม่ใช่แค่ตอน fire เท่านั้น)
        t.speed = 0.0
        t.rpm = 800

    # ── Ignition cycle: ต้องอัปเดตท้ายสุดเสมอ (override ทับ event ถ้าดับเครื่อง)
    just_switched = update_mock_ignition_cycle(idx, t)

    if just_switched:
        state_txt = (
            "ON — trip start"
            if t.ignition
            else "OFF — trip closing (debounce 30s ที่ backend)"
        )
        print(f"[Mock] device={p.device_id} ignition {state_txt} (ts={t.ts})")

    return t


# =====================================================
# Build + Sign payload — ตรงกับ buildPayloadFor() ใน main.cpp
# =====================================================
def build_payload_for(t: TelemetryData) -> dict:
    return {
        "device_id": t.device_id,
        "device_name": t.device_name,
        "ts": t.ts,
        "lat": round(t.lat, 7),
        "lon": round(t.lon, 7),
        "speed": t.speed,
        "heading": t.heading,
        "alt": t.altitude,
        "hdop": t.hdop,
        "rpm": t.rpm,
        "throttle": t.throttle,
        "engine_load": t.engine_load,
        "coolant_temp": t.coolant_temp,
        "fuel_level": t.fuel_level,
        "maf": t.maf,
        "ax": t.ax,
        "ay": t.ay,
        "az": t.az,
        "gx": t.gx,
        "gy": t.gy,
        "gz": t.gz,
        "event": t.event,
        "event_severity": t.event_severity,
        "ignition": t.ignition,
        "temperature": t.temperature,
        "humidity": t.humidity,
    }


def generate_signed_payload(data: dict, secret_key: str) -> str:
    """
    เซ็น payload ด้วย HMAC-SHA256 — เหมือน generate_signed_payload()
    เดิมใน mock_hardware_stream.py / buildSignedPayload() ใน main.cpp:
    เซ็นบน JSON string เต็ม แล้วตัด "}" ตัวสุดท้ายออก ต่อด้วย
    ,"sig":"<hex>"} เข้าไปแทน
    """
    payload_str = json.dumps(data, separators=(",", ":"))
    base_str = payload_str[:-1]

    sig = hmac.new(
        secret_key.encode("utf-8"),
        payload_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f'{base_str},"sig":"{sig}"}}'


# =====================================================
# Main loop — round-robin ส่งข้อมูล 10 บอร์ดไม่จบ
# =====================================================
async def main():
    print("🚀 เริ่มระบบจำลองการสตรีมข้อมูลจากกล่อง GPS (10 บอร์ด, round-robin)...")
    print(f"    MQTT: {MQTT_HOST}:{MQTT_PORT}")
    print(f"    Devices: {[p.device_id for p in MOCK_PROFILES]}")

    client = mqtt_client.Client(
        callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2
    )
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()

    device_index = 0
    publish_count = 0

    try:
        while True:
            profile = MOCK_PROFILES[device_index]
            topic = f"kotchasaan/fleet/{profile.device_id}/telemetry"

            t = update_mock_data(device_index)
            payload = build_payload_for(t)
            signed = generate_signed_payload(payload, HMAC_SECRET)

            ok = client.publish(topic, signed, qos=1)
            publish_count += 1

            event_txt = t.event if t.event else "-"
            print(
                f"[MQTT] #{publish_count} device={profile.device_id} "
                f"speed={t.speed:.1f} ignition={t.ignition} "
                f"event={event_txt} sev={t.event_severity:.0f}"
            )

            device_index = (device_index + 1) % len(MOCK_PROFILES)
            await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n🛑 หยุดการสตรีมข้อมูล (ผู้ใช้สั่งยกเลิก)")

    finally:
        client.loop_stop()
        client.disconnect()
        print("✅ ปิดการเชื่อมต่อ MQTT เรียบร้อย")


if __name__ == "__main__":
    asyncio.run(main())
