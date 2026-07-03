# mock_hardware_stream.py
import json
import hmac
import hashlib
import time
import asyncio
from datetime import datetime, timezone
from paho.mqtt import client as mqtt_client

MQTT_HOST = "192.168.1.43"
MQTT_PORT = 1884
MQTT_TOPIC = "kotchasaan/fleet/KTC-001/telemetry"
HMAC_SECRET = "fleet_hmac_secret_KTC001_2026"

def generate_signed_payload(data: dict, secret_key: str) -> str:
    payload_str = json.dumps(data, separators=(',', ':'))
    base_str = payload_str[:-1]

    # hmac.new() คือ API มาตรฐานของ Python stdlib (ใช้ได้ปกติ)
    sig = hmac.new(
        secret_key.encode('utf-8'),
        payload_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    final_signed_str = f'{base_str},"sig":"{sig}"}}'
    return final_signed_str

async def main():
    print("🚀 เริ่มระบบจำลองการสตรีมข้อมูลจากกล่อง GPS (KTC-Test)...")

    client = mqtt_client.Client(callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()

    # ใช้ epoch จริง ณ ปัจจุบัน
    base_ts = int(time.time())

    # ── Event 1: Ignition ON ──────────────────────────────────────
    print("\n🎬 [Event 1/4] สตาร์ทรถยนต์เพื่อเปิดทริปการเดินทาง...")
    start_payload = {
        "ts": base_ts, "device_id": "KTC-002",
        "lat": 13.7563, "lon": 100.5018,
        "speed": 0.0, "heading": 90, "alt": 10, "hdop": 0.9, "rpm": 850,
        "throttle": 0.0, "engine_load": 15.0, "coolant_temp": 85, "fuel_level": 80.0,
        "maf": 4.5, "ax": 0.0, "ay": 0.0, "az": 1.0,
        "gx": 0.0, "gy": 0.0, "gz": 0.0,
        "event": "", "event_severity": 0.0, "ignition": True,
        "temperature": 25.5, "humidity": 60.0
    }
    client.publish(MQTT_TOPIC, generate_signed_payload(start_payload, HMAC_SECRET))
    await asyncio.sleep(2)

    # ── Event 2: Speeding (ใช้ ts จริง ไม่ต้องจำลองตี 2) ──────────
    # หมายเหตุ: ถ้าอยากทดสอบ night multiplier (คะแนนหักเพิ่มช่วงตี 0-4)
    # ให้รันตอนตี 0-4 จริงๆ หรือแก้ ts ใน DB โดยตรงหลังทดสอบ
    print("🚨 [Event 2/4] วิ่งความเร็ว 110 กม./ชม. เกินเกณฑ์...")
    overspeed_payload = start_payload.copy()
    overspeed_payload.update({
        "ts": base_ts + 10,
        "speed": 110.0,
        "rpm": 3200,
        "event": "speeding",
        "event_severity": 1.0,
    })
    client.publish(MQTT_TOPIC, generate_signed_payload(overspeed_payload, HMAC_SECRET))
    await asyncio.sleep(2)

    # ── Event 3: Harsh Brake ที่ speed < 20 → ต้อง exempt ──────────
    print("🚧 [Event 3/4] เบรกกะทันหันในเขตก่อสร้าง (speed 15 km/h → exempt)...")
    braking_payload = start_payload.copy()
    braking_payload.update({
        "ts": base_ts + 20,
        "speed": 15.0,
        # event ใช้ค่า "harsh_brake" ตรงกับ schema ใน telemetry_raw และ score_calculator
        "event": "harsh_brake",
        "event_severity": 0.9,
        "ax": -0.65,
        "ay": -0.5,
    })
    client.publish(MQTT_TOPIC, generate_signed_payload(braking_payload, HMAC_SECRET))
    await asyncio.sleep(2)

    # ── Event 4: Ignition OFF → Trip End ────────────────────────────
    print("🏁 [Event 4/4] ดับเครื่องยนต์ ปิดทริปและประมวลผลคะแนน...")
    stop_payload = start_payload.copy()
    stop_payload.update({
        "ts": base_ts + 30,
        "speed": 0.0,
        "rpm": 0,
        "ignition": False,
        "event": "",
        "event_severity": 0.0,
    })
    client.publish(MQTT_TOPIC, generate_signed_payload(stop_payload, HMAC_SECRET))

    await asyncio.sleep(3)
    client.loop_stop()
    client.disconnect()
    print("\n✅ ยิงสตรีมข้อมูลทดสอบเสร็จสมบูรณ์!")

if __name__ == "__main__":
    asyncio.run(main())