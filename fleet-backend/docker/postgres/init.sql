-- =============================================================
-- Kotchasaan Fleet Telematics — Database Init Script
-- ตรงตาม FDD v1.4 Section 11.2
-- =============================================================

-- TimescaleDB Extension (ต้องทำก่อน CREATE TABLE ทุกตัว)
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- =============================================================
-- 1. DEVICES  (ต้องสร้างก่อน telemetry_raw และ trip_logs)
-- =============================================================

CREATE TABLE IF NOT EXISTS devices (
    id                   VARCHAR(20)     PRIMARY KEY,
    vehicle_id           INTEGER         UNIQUE NULL,   -- 1-to-1 binding, NULL = ยังไม่ผูกรถ
    active               BOOLEAN         DEFAULT true,
    firmware_ver         VARCHAR(50),
    registered_at        TIMESTAMPTZ     DEFAULT NOW(),
    driver_id            INTEGER,                       -- FK → Odoo hr.employee.id (cache คนขับปัจจุบัน)

    -- FDD §13 Security: "MQTT username/password per device"
    -- คอลัมน์ใหม่ NULL ได้ทั้งคู่ → ไม่กระทบ INSERT/SELECT เดิมใน routes_config.py
    -- ที่ไม่ได้ระบุค่าคอลัมน์นี้ (เช่น _register_single(), PUT /config/vehicle)
    -- ค่าจริงต้องถูกสร้าง/หมุนเวียนแยกต่างหาก (ไม่ generate ในไฟล์นี้)
    mqtt_username        VARCHAR(50),                   -- per-device MQTT username (FDD §13)
    mqtt_password_hash   VARCHAR(255)                   -- per-device MQTT password (เก็บเป็น hash เท่านั้น, FDD §13)
);

CREATE INDEX IF NOT EXISTS idx_devices_vehicle_id
    ON devices (vehicle_id)
    WHERE vehicle_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_devices_active
    ON devices (active);

-- =============================================================
-- 2. UPDATE_STATUS  (tracking การผูก device ↔ vehicle)
--    vehicle_id อ้างอิง Odoo fleet.vehicle.id โดยตรง
--    ไม่มีตาราง vehicles ในฝั่ง Backend
-- =============================================================

CREATE TABLE IF NOT EXISTS update_status (
    vehicle_id          INTEGER         NOT NULL,
    device_id           VARCHAR(20)     NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    driver_id           INTEGER,                        -- FK → Odoo hr.employee.id
    date_update_latest  TIMESTAMPTZ     DEFAULT NOW(),

    PRIMARY KEY (vehicle_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_update_status_driver
    ON update_status (driver_id)
    WHERE driver_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_update_status_device
    ON update_status (device_id);

CREATE INDEX IF NOT EXISTS idx_update_status_vehicle
    ON update_status (vehicle_id);

-- =============================================================
-- 3. USERS  (สำหรับ JWT Authentication)
-- =============================================================

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL          PRIMARY KEY,
    username        VARCHAR(50)     UNIQUE NOT NULL,
    email           VARCHAR(100)    UNIQUE NOT NULL,
    hashed_password VARCHAR(255)    NOT NULL,
    full_name       VARCHAR(100),
    is_active       BOOLEAN         DEFAULT true,
    role            VARCHAR(20)     DEFAULT 'user',   -- user | manager | admin
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
CREATE INDEX IF NOT EXISTS idx_users_email    ON users (email);

-- =============================================================
-- 4. API_KEYS  (สำหรับ API Key Authentication)
-- =============================================================

CREATE TABLE IF NOT EXISTS api_keys (
    id          SERIAL          PRIMARY KEY,
    key_hash    VARCHAR(255)    UNIQUE NOT NULL,
    name        VARCHAR(100),
    created_by  INTEGER         REFERENCES users(id) ON DELETE SET NULL,
    is_active   BOOLEAN         DEFAULT true,
    created_at  TIMESTAMPTZ     DEFAULT NOW(),
    last_used   TIMESTAMPTZ,
    scope       VARCHAR(20)     DEFAULT 'general'
);

-- =============================================================
-- 5. TELEMETRY_RAW  (Hypertable — partition by ts)
--    ตรงตาม FDD v1.4 Section 11.2
-- =============================================================

CREATE TABLE IF NOT EXISTS telemetry_raw (
    id              BIGSERIAL,
    device_id       VARCHAR(20)     NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ     NOT NULL,           -- partition key

    -- TimescaleDB requires ts in PRIMARY KEY when using create_hypertable
    PRIMARY KEY (id, ts),

    -- GPS
    lat             DOUBLE PRECISION,
    lon             DOUBLE PRECISION,
    speed           REAL,
    heading         SMALLINT,                           -- 0-359 degrees
    altitude        REAL,
    hdop            REAL,

    -- OBD-II
    rpm             SMALLINT,
    throttle        REAL,                               -- %
    engine_load     REAL,                               -- % (เพิ่มจาก fleet_db.sql)
    coolant_temp    REAL,                               -- °C (เพิ่มจาก fleet_db.sql)
    fuel_level      REAL,                               -- %
    maf_airflow     REAL,                               -- g/s MAF sensor (เพิ่มจาก fleet_db.sql)

    -- IMU
    ax              REAL,   ay REAL,   az REAL,         -- Accelerometer (G)
    gx              REAL,   gy REAL,   gz REAL,         -- Gyroscope (°/s)

    -- Events
    event           VARCHAR(30),                        -- null ถ้าปกติ
    event_severity  REAL,                               -- 0-100

    -- Engine
    ignition        BOOLEAN,

    -- Metadata
    created_at      TIMESTAMPTZ     DEFAULT NOW()       -- เวลา insert ลง DB (เพิ่มจาก fleet_db.sql)
);

-- แปลงเป็น TimescaleDB hypertable (partition by ts)
SELECT create_hypertable('telemetry_raw', 'ts', if_not_exists => TRUE);

-- =============================================================
-- DATA RETENTION — FDD v1.4 §13 Non-Functional Requirements
-- "เก็บ raw telemetry 90 วัน, trip summary 3 ปี, incentive records
--  ตลอดชีวิต"
--
-- telemetry_raw เป็น TimescaleDB hypertable จึงใช้ native retention
-- policy ได้โดยตรง — TimescaleDB จะ drop chunk ที่เก่ากว่า 90 วัน
-- โดยอัตโนมัติผ่าน background job (ไม่ต้องเขียน cron/DELETE เอง)
--
-- if_not_exists => TRUE ทำให้คำสั่งนี้ idempotent: รันซ้ำได้โดยไม่ error
-- แม้ policy จะถูกสร้างไปแล้วจากการรัน init.sql ครั้งก่อน (เช่นตอน
-- container restart กับ volume เดิม)
--
-- สำคัญ: policy นี้ "ไม่ลบข้อมูลที่มีอยู่ทันที" — TimescaleDB จะ drop
-- เฉพาะ chunk ที่ครบกำหนดตามรอบ background job ของมันเอง แถวที่ยัง
-- ไม่เก่าเกิน 90 วัน ณ ตอนนี้จะไม่ถูกแตะต้องจนกว่าจะถึงเวลาจริงในอนาคต
--
-- ตรวจสอบ/จัดการ policy นี้ภายหลังได้ด้วย:
--   SELECT * FROM timescaledb_information.jobs
--     WHERE proc_name = 'policy_retention';
--   SELECT remove_retention_policy('telemetry_raw');  -- ถ้าต้องการปิด
-- =============================================================

SELECT add_retention_policy(
    'telemetry_raw',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- หมายเหตุ: trip_logs เป็นตารางปกติ (ไม่ใช่ hypertable) จึงใช้
-- add_retention_policy() ของ TimescaleDB ไม่ได้ — การลบ trip_logs
-- ที่เก่ากว่า 3 ปีตาม FDD §13 ถูกจัดการแยกในฝั่ง application layer
-- (ดู trip_logs_retention_task() ใน app/main.py) ซึ่งรันเป็น background
-- asyncio task ทุก 24 ชั่วโมงแทน

-- หมายเหตุ: "incentive records เก็บตลอดชีวิต" ตาม FDD §13 หมายถึง
-- fleet.telematics.incentive model ซึ่งอยู่ฝั่ง Odoo (§12.4) ไม่ใช่
-- ตารางใน Backend database นี้ — จึงไม่มี retention policy ใดๆ
-- สำหรับ incentive records ในไฟล์นี้โดยเจตนา (ไม่มีอะไรให้ตั้งค่า
-- ที่นี่ — ข้อมูลนี้ไม่เคยอยู่ใน TimescaleDB schema ของ Backend เลย)

CREATE INDEX IF NOT EXISTS idx_telemetry_device_ts
    ON telemetry_raw (device_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_event
    ON telemetry_raw (event)
    WHERE event IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_telemetry_ignition
    ON telemetry_raw (ignition)
    WHERE ignition IS NOT NULL;

-- =============================================================
-- 6. TRIP_LOGS  (ตรงตาม FDD v1.4 Section 11.2)
-- =============================================================

CREATE TABLE IF NOT EXISTS trip_logs (
    id                  BIGSERIAL       PRIMARY KEY,
    device_id           VARCHAR(20)     NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    vehicle_id          INTEGER         NOT NULL,       -- FK → Odoo fleet.vehicle.id
    driver_id           INTEGER,                        -- FK → Odoo hr.employee.id

    trip_start          TIMESTAMPTZ     NOT NULL,
    trip_end            TIMESTAMPTZ,

    distance_km         REAL,
    duration_min        REAL,                           -- เวลาขับรวม (นาที)
    idle_min            REAL,                           -- เวลาจอดติดเครื่อง (นาที)
    max_speed           REAL,
    avg_speed           REAL,

    harsh_brake_count   SMALLINT        DEFAULT 0,
    harsh_accel_count   SMALLINT        DEFAULT 0,
    harsh_corner_count  SMALLINT        DEFAULT 0,
    speeding_count      SMALLINT        DEFAULT 0,

    driver_score        REAL            DEFAULT 100.0,
    fuel_used           REAL,                           -- ลิตร (ประมาณ)
    gps_track           JSONB,                          -- Array จุด GPS ตลอดเส้นทาง

    synced_to_odoo      BOOLEAN         DEFAULT false,
    synced_at           TIMESTAMPTZ,                    -- เวลาที่ Odoo รับสำเร็จ
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trip_logs_device
    ON trip_logs (device_id);

CREATE INDEX IF NOT EXISTS idx_trip_logs_vehicle
    ON trip_logs (vehicle_id);

CREATE INDEX IF NOT EXISTS idx_trip_logs_driver
    ON trip_logs (driver_id);

CREATE INDEX IF NOT EXISTS idx_trip_logs_trip_start
    ON trip_logs (trip_start DESC);

CREATE INDEX IF NOT EXISTS idx_trip_logs_driver_score
    ON trip_logs (driver_score);

CREATE INDEX IF NOT EXISTS idx_trip_logs_synced
    ON trip_logs (synced_to_odoo)
    WHERE synced_to_odoo = false;

-- =============================================================
-- 7. SCORING_CONFIG_CACHE  (ตรงตาม FDD v1.4 Section 11.2)
--    Backend cache config ล่าสุดจาก Odoo
--    Odoo push มาที่ POST /api/v1/config/scoring
-- =============================================================

CREATE TABLE IF NOT EXISTS scoring_config_cache (
    id                  SERIAL          PRIMARY KEY,
    config_name         VARCHAR(100)    NOT NULL,
    effective_date      DATE,

    -- คะแนนเริ่มต้น
    score_base          REAL            DEFAULT 100.0,

    -- น้ำหนักตัดคะแนน
    harsh_brake_deduct  REAL            DEFAULT 5.0,
    harsh_accel_deduct  REAL            DEFAULT 3.0,
    harsh_corner_deduct REAL            DEFAULT 3.0,
    speeding_deduct     REAL            DEFAULT 10.0,
    idling_deduct       REAL            DEFAULT 2.0,
    bump_deduct         REAL            DEFAULT 4.0,
    max_deduct_per_trip REAL            DEFAULT 50.0,

    -- Threshold G-force
    harsh_brake_g       REAL            DEFAULT 0.40,
    harsh_accel_g       REAL            DEFAULT 0.40,
    harsh_corner_g      REAL            DEFAULT 0.40,

    -- Threshold อื่น
    speeding_kmh_over   REAL            DEFAULT 20.0,  -- km/h เกิน limit
    idle_min_threshold  REAL            DEFAULT 5.0,   -- นาที

    synced_from_odoo_at TIMESTAMPTZ,
    is_active           BOOLEAN         DEFAULT false,
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scoring_active
    ON scoring_config_cache (is_active)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_scoring_effective_date
    ON scoring_config_cache (effective_date DESC);

-- =============================================================
-- 8. VIEW: v_device_latest_position
--    ตำแหน่งล่าสุดของ device ทุกตัว
-- =============================================================

CREATE OR REPLACE VIEW v_device_latest_position AS
SELECT
    us.vehicle_id,
    us.device_id,
    us.date_update_latest,
    d.active,
    t.ts        AS last_seen,
    t.lat,
    t.lon,
    t.speed,
    t.heading,
    t.ignition,
    t.event
FROM update_status us
LEFT JOIN devices d ON d.id = us.device_id
LEFT JOIN LATERAL (
    SELECT ts, lat, lon, speed, heading, ignition, event
    FROM telemetry_raw
    WHERE device_id = us.device_id
    ORDER BY ts DESC
    LIMIT 1
) t ON true;

-- =============================================================
-- 9. VIEW: v_driver_monthly_summary
--    สรุปคะแนนพนักงานรายเดือน
-- =============================================================

CREATE OR REPLACE VIEW v_driver_monthly_summary AS
SELECT
    driver_id,
    TO_CHAR(DATE_TRUNC('month', trip_start), 'YYYY-MM')    AS month,
    COUNT(*)                                                AS total_trips,
    ROUND(AVG(driver_score)::NUMERIC, 2)                    AS avg_score,
    ROUND(SUM(distance_km)::NUMERIC, 2)                     AS total_distance_km,
    ROUND(SUM(idle_min)::NUMERIC, 2)                        AS total_idle_min,
    SUM(harsh_brake_count)                                  AS total_harsh_brake,
    SUM(harsh_accel_count)                                  AS total_harsh_accel,
    SUM(harsh_corner_count)                                 AS total_harsh_corner,
    SUM(speeding_count)                                     AS total_speeding,
    SUM(CASE WHEN driver_score >= 85 THEN 1 ELSE 0 END)    AS safe_trips
FROM trip_logs
WHERE driver_id IS NOT NULL
GROUP BY driver_id, DATE_TRUNC('month', trip_start);

-- =============================================================
-- 10. SEED DATA — Default scoring config
-- =============================================================

INSERT INTO scoring_config_cache (
    config_name,
    effective_date,
    score_base,
    harsh_brake_deduct, harsh_accel_deduct, harsh_corner_deduct,
    speeding_deduct, idling_deduct, bump_deduct, max_deduct_per_trip,
    harsh_brake_g, harsh_accel_g, harsh_corner_g,
    speeding_kmh_over, idle_min_threshold,
    is_active
) VALUES (
    'FDD v1.4 Default',
    CURRENT_DATE,
    100.0,
    5.0, 3.0, 3.0,
    10.0, 2.0, 4.0, 50.0,
    0.40, 0.40, 0.40,
    20.0, 5.0,
    true
) ON CONFLICT DO NOTHING;

-- Seed devices KTC-001 ถึง KTC-010 (ยังไม่ผูกรถ)
-- mqtt_username / mqtt_password_hash เป็น placeholder เท่านั้น (FDD §13)
-- ต้องถูกแทนที่ด้วยค่าจริงก่อนใช้งานจริง — ห้ามใช้ค่านี้ใน production
INSERT INTO devices (id, vehicle_id, active, mqtt_username, mqtt_password_hash)
SELECT
    'KTC-' || LPAD(n::TEXT, 3, '0'),
    NULL,
    true,
    'KTC-' || LPAD(n::TEXT, 3, '0'),   -- mqtt_username = device id (placeholder)
    'PLACEHOLDER_HASH_CHANGE_ME'        -- mqtt_password_hash (placeholder — ต้อง rotate ก่อนใช้จริง)
FROM generate_series(1, 10) AS n
ON CONFLICT DO NOTHING;