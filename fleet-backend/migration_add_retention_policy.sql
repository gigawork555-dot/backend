-- =============================================================
-- Migration: Add data retention policy for telemetry_raw
-- FDD v1.4 §13 — "เก็บ raw telemetry 90 วัน"
--
-- ใช้ไฟล์นี้กับฐานข้อมูล production ที่รันอยู่แล้ว (มีข้อมูลเก่าอยู่)
-- แทนการรัน init.sql ทับใหม่ทั้งไฟล์ — init.sql จะรันอัตโนมัติแค่ตอน
-- container สร้าง volume ใหม่เท่านั้น (docker-entrypoint-initdb.d)
-- ถ้า volume มีอยู่แล้ว init.sql จะไม่ถูกรันซ้ำโดย Postgres เอง
--
-- Migration นี้:
--   - ไม่ DROP / ไม่ TRUNCATE ตารางใดๆ ทั้งสิ้น
--   - ไม่ลบแถวใดๆ ทันที — แค่ "ลงทะเบียน" policy กับ TimescaleDB
--     เท่านั้น ตัว background job ของ TimescaleDB จะเป็นผู้ drop
--     chunk ที่เก่าเกิน 90 วันเองตามรอบในอนาคต
--   - idempotent (if_not_exists => TRUE) — รันซ้ำได้อย่างปลอดภัย
-- =============================================================

SELECT add_retention_policy(
    'telemetry_raw',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- ตรวจสอบว่า policy ถูกสร้างสำเร็จ:
SELECT * FROM timescaledb_information.jobs
WHERE proc_name = 'policy_retention';
