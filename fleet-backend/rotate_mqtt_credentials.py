# rotate_mqtt_credentials.py
"""
สคริปต์หมุนเวียน (rotate) MQTT username/password ให้ device KTC-001 ถึง
KTC-010 ที่ seed ไว้ใน init.sql (ปัจจุบันยังเป็นค่า placeholder) ทั้งหมด
ในครั้งเดียว โดยเรียก endpoint

    POST /api/v1/config_device/{device_id}/mqtt-credential

ที่เพิ่มใหม่ใน app/api/routes_config.py (FDD v1.4 §13)

รหัสผ่าน plaintext จะถูก print ออกทาง stdout เท่านั้น — สคริปต์นี้
"ไม่เขียนลงไฟล์" ตามที่ตกลงไว้ ผู้ใช้ต้อง copy ไปเก็บ/ตั้งค่าในบอร์ด
เอง (ผ่าน nvsWriteCredentials() ใน security.h) ทันทีหลังรัน เพราะ
ระบบไม่เก็บ plaintext ไว้ที่ไหนอีกเลยหลังจากนี้

วิธีใช้:

    # รันจากข้างใน backend container (แนะนำ — เชื่อม DB ผ่าน internal
    # network ได้ตรงๆ เหมือน create_user.py):
    docker compose run --rm backend python rotate_mqtt_credentials.py

    # หรือรันจาก host ตรงๆ (ต้องตั้ง BASE_URL ให้ชี้ port ที่ expose จริง):
    BASE_URL=http://localhost:8001 APIKEY=ktc-fleet-2026-secret \
        python rotate_mqtt_credentials.py

Environment variables:
    BASE_URL   URL ของ backend (default: http://backend:8000 — ใช้ตอนรัน
               ผ่าน docker compose run ซึ่งอยู่ใน network เดียวกับ
               service "backend")
    APIKEY     API key สำหรับผ่าน Security(verify_api_key) — ต้องเป็น
               key ที่ active อยู่จริงในตาราง api_keys (ดู FDD §13 /
               developer portal — 1 user = 1 API key)
    DEVICE_PREFIX / DEVICE_COUNT  ปรับช่วง device ที่ต้อง rotate ได้
               (default: KTC- ตั้งแต่ 001 ถึง 010 ตรงกับ seed ใน init.sql)
"""

import os
import sys

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://backend:8000")
API_KEY = os.environ.get("APIKEY", "")
DEVICE_PREFIX = os.environ.get("DEVICE_PREFIX", "KTC-")
DEVICE_COUNT = int(os.environ.get("DEVICE_COUNT", "10"))


def rotate_all() -> None:
    if not API_KEY:
        print(
            "❌ ไม่พบ APIKEY — กรุณาตั้ง environment variable APIKEY "
            "ก่อนรันสคริปต์นี้ (ต้องเป็น API key ที่ active อยู่จริง)"
        )
        sys.exit(1)

    device_ids = [
        f"{DEVICE_PREFIX}{str(n).zfill(3)}" for n in range(1, DEVICE_COUNT + 1)
    ]

    print(f"🔐 เริ่ม rotate MQTT credential ให้ {len(device_ids)} device: "
          f"{device_ids[0]} ... {device_ids[-1]}")
    print(f"    Backend: {BASE_URL}")
    print("=" * 72)

    results = []
    failed = []

    with httpx.Client(base_url=BASE_URL, headers={"APIKEY": API_KEY}, timeout=15.0) as client:
        for device_id in device_ids:
            try:
                resp = client.post(f"/api/v1/config_device/{device_id}/mqtt-credential")

                if resp.status_code != 200:
                    print(f"❌ {device_id}: HTTP {resp.status_code} — {resp.text}")
                    failed.append(device_id)
                    continue

                data = resp.json()
                results.append(data)

                print(
                    f"✅ {data['device_id']:<10} "
                    f"username={data['mqtt_username']:<10} "
                    f"password={data['mqtt_password']}"
                )

            except Exception as e:
                print(f"❌ {device_id}: เชื่อมต่อ backend ล้มเหลว — {e}")
                failed.append(device_id)

    print("=" * 72)
    print(f"สรุป: สำเร็จ {len(results)} | ล้มเหลว {len(failed)}")
    if failed:
        print(f"⚠️  device ที่ rotate ไม่สำเร็จ: {', '.join(failed)}")

    print(
        "\n⚠️  คัดลอกรหัสผ่านด้านบนไปตั้งค่าในบอร์ดทันที (ผ่าน "
        "nvsWriteCredentials() ใน security.h) — ระบบจะไม่แสดงค่านี้ซ้ำอีก "
        "และไม่มีการเก็บ plaintext ไว้ที่ไหนเลยหลังจากนี้"
    )

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    rotate_all()
