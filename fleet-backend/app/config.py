# app/config.py
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Database Settings (TimescaleDB)
    DB_HOST: str
    DB_PORT: int
    DB_NAME: str
    DB_USER: str
    DB_PASS: str

    # MQTT Broker Settings
    MQTT_HOST: str
    MQTT_PORT: int
    MQTT_USER: str = "admin"   # ค่า Default ของ EMQX
    MQTT_PASS: str = "public"  # ค่า Default ของ EMQX
    MQTT_TOPIC: str

    # FDD §13 Security — "MQTT over TLS 1.2"
    # ค่า default ต้องเป็น False เพื่อไม่กระทบ dev environment ปัจจุบัน
    # ที่ใช้ plain MQTT (port 1883/1884) — เปิดใช้จริงด้วยการตั้งค่าใน .env
    # การ generate/configure TLS cert ฝั่ง broker เป็นงาน infra แยกต่างหาก
    # ไม่ใช่ส่วนของโค้ดนี้
    MQTT_TLS_ENABLED: bool = False
    MQTT_CA_CERT_PATH: Optional[str] = None

    # รหัสลับสำหรับตรวจสอบความถูกต้องข้อมูล (ต้องตรงกับฝั่ง ESP32)
    HMAC_SECRET: str = "fleet_hmac_secret_KTC001_2026"

    # โดดค่าจากไฟล์ .env โดยอัตโนมัติ
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()