#include <Arduino.h>
#include <WiFiClient.h>   // [FIX] plain client -- ไม่ใช้ TLS กับ local broker
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <Wire.h>
#include <Arduino_GFX_Library.h>
#include <nvs_flash.h>

#include "config.h" 
#include "security.h"
// #include "ca_cert.h"   // [FIX] ปิดไว้ -- ไม่ได้ใช้ TLS กับ local broker แล้ว
#include "nvs_provision.h"

// =============================================
//  COLORS
// =============================================
inline uint16_t S(uint16_t c) { return c; }
const uint16_t CLR_BG     = S(0x0841);
const uint16_t CLR_CARD   = S(0x1082);
const uint16_t CLR_ACCENT = S(0x05FD);
const uint16_t CLR_GREEN  = S(0x07E0);
const uint16_t CLR_RED    = S(0xF800);
const uint16_t CLR_YELLOW = S(0xFFE0);
const uint16_t CLR_ORANGE = S(0xFD20);
const uint16_t CLR_WHITE  = 0xFFFF;
const uint16_t CLR_GRAY   = S(0x8410);
const uint16_t CLR_TEAL   = S(0x0439);

// =============================================
//  DISPLAY
// =============================================
Arduino_DataBus *bus = new Arduino_ESP32QSPI(
    TFT_CS, TFT_SCK, TFT_D0, TFT_D1, TFT_D2, TFT_D3);
Arduino_GFX *g = new Arduino_AXS15231B(bus, TFT_RST, 0, false, 320, 480);
Arduino_Canvas *gfx = new Arduino_Canvas(320, 480, g, 0, 0, 0);

// =============================================
//  MQTT / WiFi (plain -- local EMQX broker ไม่มี TLS listener)
// =============================================

WiFiClient   wifiClient;   // [FIX] plain (ไม่เข้ารหัส) -- ตรงกับ MQTT_TLS_ENABLED=False ของ backend dev
PubSubClient mqttClient(wifiClient);

// MQTT Topics (สร้างจาก device_id)
char TOPIC_TELEMETRY[64];
char TOPIC_STATUS[64];
char TOPIC_CONFIG[64];
char MQTT_CLIENT_ID[32];

// =============================================
//  TELEMETRY
// =============================================
struct TelemetryData {
    String device_id;
    String device_name;
    float  lat = 0, lon = 0, speed = 0;
    int    heading = 0;
    float  altitude = 0, hdop = 1.0;
    int    rpm = 0;
    float  throttle = 0, engine_load = 0;
    float  coolant_temp = 0, fuel_level = 0, maf = 0;
    float  ax = 0, ay = 0, az = 9.8;
    float  gx = 0, gy = 0, gz = 0;
    String event = "";
    float  event_severity = 0;
    bool   ignition = false;
    float  temperature = 0, humidity = 0;
    unsigned long ts = 0;
};
TelemetryData tele;

unsigned long lastPublish  = 0;
unsigned long lastMockTick = 0;
unsigned long publishCount = 0;
bool          displayDirty = true;

// =============================================
//  MOCK DATA — บอร์ดเดียว KTC-222 (เชียงใหม่ รอบประตูท่าแพ)
//
//  [แก้ไขรอบนี้ — deterministic round-robin]
//  เดิม: สุ่มเลือก event ด้วย random(0,100) แบ่งเปอร์เซ็นต์ normal/harsh/
//  speeding/idling — ปัญหาคือ idling เป็น sustained-state ค้างยาว 6-10
//  นาทีจำลองทุกครั้งที่สุ่มโดน ทำให้ "ครองเวลา" ส่วนใหญ่ไป ดูเหมือนได้แต่
//  idling ทั้งที่ % การสุ่มถูกต้องอยู่แล้ว (ปัญหาเรื่อง time-share ไม่ใช่
//  เรื่อง probability)
//
//  ใหม่: เลิกสุ่มเลือก event เปลี่ยนเป็น "วนรอบตายตัว" (round-robin) ผ่าน
//  MockPhase ตามลำดับคงที่ทุกครั้ง คนละ tick-budget คงที่ที่กำหนดไว้ล่วงหน้า
//  ไม่มีการสุ่มว่าจะเป็น event ไหนอีกต่อไป — ทุก event (brake/accel/corner/
//  bump/speeding/idling) ได้ "โควตาเวลา" เท่ากันเป๊ะตามที่ตั้งไว้ วนซ้ำไป
//  เรื่อยๆ สุ่มที่เหลือมีแค่ noise เล็กๆ ในตัวเลข sensor (deterministic
//  sine wave แทน) ไม่กระทบลำดับ/สัดส่วน event
//
//  speeding threshold คงที่ = 90 km/h (MOCK_SPEEDING_KMH) ตามที่กำหนด —
//  ไม่มีการสุ่มค่าความเร็วในช่วง speeding อีกต่อไป
//
//  ตำแหน่ง GPS ยังคงเป็น random-walk ต่อเนื่อง (ไม่ใช่ teleport) เพื่อให้
//  ระยะทางสะสม (haversine) ที่ trip_manager.py คำนวณยังสมจริง — เปลี่ยน
//  จาก random heading drift เป็น sine wave แบบ deterministic
// =============================================
#ifdef ENABLE_MOCK_DATA

// ── จุดศูนย์กลาง + รัศมีพื้นที่ที่รถวิ่งวนอยู่ (เชียงใหม่ — ประตูท่าแพ) ──
// พื้นที่แคบกว่ากรุงเทพฯ มาก (เขตเมืองเก่า/คูเมือง) และความเร็วเฉลี่ยต่ำกว่า
// เพราะถนนแคบ รถเยอะ มีนักท่องเที่ยวเดินเท้า/สามล้อ
static const float MOCK_CENTER_LAT   = 18.7877f;   // ประตูท่าแพ โดยประมาณ
static const float MOCK_CENTER_LON   = 98.9931f;
// [FIX] ขยายรัศมีพื้นที่วิ่งวนรอบประตูท่าแพให้กว้างขึ้น — เดิม 0.015°
// (~1.5-1.7 กม.) แคบเกินไป ครอบคลุมแค่ในคูเมืองเก่าบางส่วนเท่านั้น
// ขยับเป็น 0.04° (~4.4-4.5 กม.) ให้ครอบคลุมเขตเมืองเก่าทั้งหมด
// (ประตูท่าแพ, ประตูช้างเผือก, ประตูสวนดอก ฯลฯ) ไปจนถึงพื้นที่รอบนอก
// คูเมืองเล็กน้อย ใกล้เคียงสัดส่วนพื้นที่ของกรุงเทพฯ (0.07°) มากขึ้น
// แต่ยังคงแคบกว่าเพราะเชียงใหม่เป็นเมืองเล็กกว่า
static const float MOCK_AREA_RADIUS_DEG = 0.04f;   // ~4.4-4.5 กม. รอบจุดศูนย์กลาง
static const float MOCK_BASE_SPEED_KMH  = 25.0f;   // ถนนแคบเขตเมืองเก่า ช้ากว่ากรุงเทพฯ

// [FIX] speeding threshold ตายตัวตามที่กำหนด = 90 km/h
static const float MOCK_SPEEDING_KMH = 90.0f;

// ── [FIX] วนรอบ event แบบตายตัว (deterministic round-robin) ─────────
// ไม่มีการสุ่มว่าจะเป็น event อะไรอีกต่อไป — ทุก event ได้ "โควตาเวลา"
// เท่ากันเป๊ะตามลำดับ ไล่ไปเรื่อยๆ วนซ้ำ ไม่มีทางค้างที่ event ใด
// event หนึ่งนานผิดปกติเหมือนเดิม (ปัญหาเดิม: idling สุ่มโดนแล้วค้าง
// 6-10 นาที ครองเวลาส่วนใหญ่ ทำให้ดูเหมือน "ได้แต่ idling")
enum MockPhase {
    MOCK_PHASE_NORMAL_1 = 0,
    MOCK_PHASE_HARSH_BRAKE,
    MOCK_PHASE_NORMAL_2,
    MOCK_PHASE_HARSH_ACCEL,
    MOCK_PHASE_NORMAL_3,
    MOCK_PHASE_HARSH_CORNER,
    MOCK_PHASE_NORMAL_4,
    MOCK_PHASE_BUMP,
    MOCK_PHASE_NORMAL_5,
    MOCK_PHASE_SPEEDING,
    MOCK_PHASE_NORMAL_6,
    MOCK_PHASE_IDLING,
    MOCK_PHASE_COUNT
};

// ระยะเวลา (วินาที) ของแต่ละ phase — ปรับตัวเลขได้ตามต้องการ แต่ต้อง
// เป็นค่าคงที่เสมอ ห้าม random() เด็ดขาดตรงนี้
static const int MOCK_PHASE_DURATION_SEC[MOCK_PHASE_COUNT] = {
    /* NORMAL_1      */ 15,
    /* HARSH_BRAKE   */  3,
    /* NORMAL_2      */ 15,
    /* HARSH_ACCEL   */  3,
    /* NORMAL_3      */ 15,
    /* HARSH_CORNER  */  3,
    /* NORMAL_4      */ 15,
    /* BUMP          */  3,
    /* NORMAL_5      */ 15,
    /* SPEEDING      */  5,
    /* NORMAL_6      */ 15,
    /* IDLING        */ 60,   // [FIX] fixed 60s แทน random 6-10 นาที เดิม
};

int mockPhase          = MOCK_PHASE_NORMAL_1;
int mockPhaseTicksLeft  = MOCK_PHASE_DURATION_SEC[MOCK_PHASE_NORMAL_1];
unsigned long mockTickCount = 0;

// ── ตำแหน่ง/heading แบบ random-walk (state ต่อเนื่องข้าม tick) ───────
float mockLat          = MOCK_CENTER_LAT;
float mockLon          = MOCK_CENTER_LON;
float mockHeadingDeg   = 0.0f;
bool  mockPositionInit = false;

static const float MOCK_DEG_TO_RAD     = 0.0174533f;
static const float MOCK_KM_PER_DEG_LAT = 111.32f;

// คำนวณ bearing (0-359°) จากจุดปัจจุบันไปจุดศูนย์กลาง — ใช้ตอนใกล้ขอบ
// bounding box เพื่อ "เลี้ยว" กลับเข้าพื้นที่แทนการหลุดออกไปเรื่อยๆ
static float mockBearingToCenter() {
    float dLat = MOCK_CENTER_LAT - mockLat;
    float dLon = MOCK_CENTER_LON - mockLon;
    float bearing = atan2f(dLon, dLat) / MOCK_DEG_TO_RAD;   // atan2(x=lon,y=lat)
    if (bearing < 0) bearing += 360.0f;
    return bearing;
}

// เดินตำแหน่งจริง 1 tick (1 วินาที) ตาม speed(km/h) ปัจจุบัน + heading
// พร้อม bounding box กันหลุดพื้นที่ — เรียกเฉพาะตอนไม่ได้ idling
static void mockAdvancePosition(float speedKmh) {
    if (!mockPositionInit) {
        mockLat          = MOCK_CENTER_LAT;
        mockLon          = MOCK_CENTER_LON;
        mockHeadingDeg   = 45.0f;   // [FIX] ค่าเริ่มต้นตายตัว ไม่สุ่ม
        mockPositionInit = true;
    }

    // [FIX] heading drift แบบ deterministic (sine wave) แทนสุ่มมุมทุก tick
    mockHeadingDeg += 5.0f * sinf((float)tele.ts * 0.05f);
    if (mockHeadingDeg < 0)    mockHeadingDeg += 360.0f;
    if (mockHeadingDeg >= 360) mockHeadingDeg -= 360.0f;

    // ระยะทางที่เดินได้ใน 1 tick (km) จาก speed จริง ไม่ใช่ค่าคงที่/สุ่มลอยๆ
    float distanceKm = speedKmh * (1.0f / 3600.0f);   // dt = 1 วินาที

    float headingRad  = mockHeadingDeg * MOCK_DEG_TO_RAD;
    float kmPerDegLon  = MOCK_KM_PER_DEG_LAT * cosf(mockLat * MOCK_DEG_TO_RAD);
    if (kmPerDegLon < 1.0f) kmPerDegLon = 1.0f;  // กัน div-by-near-zero (ไม่เกิดจริงในไทย)

    mockLat += (distanceKm / MOCK_KM_PER_DEG_LAT) * cosf(headingRad);
    mockLon += (distanceKm / kmPerDegLon)         * sinf(headingRad);

    // ── Bounding box: ถ้าหลุดรัศมีพื้นที่ ให้ "เลี้ยว" กลับเข้าเมือง ──
    float distFromCenterDeg = sqrtf(
        (mockLat - MOCK_CENTER_LAT) * (mockLat - MOCK_CENTER_LAT) +
        (mockLon - MOCK_CENTER_LON) * (mockLon - MOCK_CENTER_LON)
    );

    if (distFromCenterDeg > MOCK_AREA_RADIUS_DEG) {
        // ดึงตำแหน่งกลับมาที่ขอบพอดี (กันหลุดไปไกลกว่านี้ในรอบเดียว)
        float scale = MOCK_AREA_RADIUS_DEG / distFromCenterDeg;
        mockLat = MOCK_CENTER_LAT + (mockLat - MOCK_CENTER_LAT) * scale;
        mockLon = MOCK_CENTER_LON + (mockLon - MOCK_CENTER_LON) * scale;

        // [FIX] เลี้ยวกลับเข้าเมืองตรงๆ แบบ deterministic ไม่สุ่มเบี่ยง
        mockHeadingDeg = mockBearingToCenter();
    }

    tele.lat     = mockLat;
    tele.lon     = mockLon;
    tele.heading = (int)mockHeadingDeg;
}

// ── Ignition ON/OFF cycle state (ดับเครื่องจริงเป็นช่วงๆ) ────────
// หมายเหตุ: ตรงนี้ยังคง random ไว้เพราะไม่กระทบสัดส่วน event ที่ขอแก้
// (คนละกลไกกับ MockPhase) และ trip length ที่หลากหลายมีประโยชน์ต่อ
// การทดสอบ trip_manager (debounce 30s ฝั่ง backend)
#define MOCK_IGNITION_ON_MIN_SECONDS    60
#define MOCK_IGNITION_ON_MAX_SECONDS   300
#define MOCK_IGNITION_OFF_MIN_SECONDS   45   // > 30s debounce ฝั่ง backend เสมอ
#define MOCK_IGNITION_OFF_MAX_SECONDS  150

bool          mockIgnitionOn      = true;
unsigned long mockStateStartedTs  = 0;
unsigned long mockCurrentDuration = 0;
bool          mockIgnitionInit    = false;

static unsigned long mockRandomStateDuration(bool ignitionOn) {
    return ignitionOn
        ? (unsigned long)random(MOCK_IGNITION_ON_MIN_SECONDS,  MOCK_IGNITION_ON_MAX_SECONDS + 1)
        : (unsigned long)random(MOCK_IGNITION_OFF_MIN_SECONDS, MOCK_IGNITION_OFF_MAX_SECONDS + 1);
}

// อัปเดตสถานะ ignition ตาม ts ปัจจุบัน — คืน true ถ้าเพิ่งสลับสถานะ
static bool updateMockIgnitionCycle() {
    if (!mockIgnitionInit) {
        mockIgnitionOn      = true;   // [FIX] เริ่มต้นวิ่งเสมอ (deterministic)
        mockCurrentDuration = mockRandomStateDuration(mockIgnitionOn);
        mockStateStartedTs  = tele.ts;
        mockIgnitionInit    = true;
    }

    unsigned long elapsed = tele.ts - mockStateStartedTs;
    bool justSwitched = false;

    if (elapsed >= mockCurrentDuration) {
        mockIgnitionOn      = !mockIgnitionOn;
        mockStateStartedTs  = tele.ts;
        mockCurrentDuration = mockRandomStateDuration(mockIgnitionOn);
        justSwitched        = true;

        // ดับเครื่อง → รีเซ็ต phase cycle กลับจุดเริ่มเสมอ กันค้าง
        // event/idling ข้ามรอบ ignition แบบผิดธรรมชาติ
        if (!mockIgnitionOn) {
            mockPhase          = MOCK_PHASE_NORMAL_1;
            mockPhaseTicksLeft = MOCK_PHASE_DURATION_SEC[mockPhase];
        }
    }

    tele.ignition = mockIgnitionOn;

    if (!mockIgnitionOn) {
        // ดับเครื่องจริง — นิ่งสนิท ห้ามมี event ใดๆ
        tele.speed = 0.0f; tele.rpm = 0; tele.throttle = 0.0f;
        tele.event = ""; tele.event_severity = 0.0f;
        tele.ax = 0.0f; tele.ay = 0.0f; tele.az = 1.0f;
        tele.gx = 0.0f; tele.gy = 0.0f; tele.gz = 0.0f;
    }

    return justSwitched;
}

void updateMockData() {
    if (millis() - lastMockTick < 1000) return;
    lastMockTick = millis();
    mockTickCount++;

    tele.ts = millis() / 1000;

    // [FIX] ค่า housekeeping เปลี่ยนจาก random() เป็น deterministic
    // (mod ของ tick count) เพื่อยังดูมีความหลากหลายแต่ไม่มีการสุ่มแท้จริง
    tele.altitude     = 300.0f + (mockTickCount % 40);        // เชียงใหม่ที่ราบสูง (~310m MSL)
    tele.hdop         = 0.9f + ((mockTickCount % 5) * 0.1f);
    // [FIX] เลขบวมผิดปกติ (เช่น 42222546) ใน coolant/temperature/humidity —
    // mockTickCount เป็น unsigned long ดังนั้น (mockTickCount % N) ก็เป็น
    // unsigned long ด้วย เดิมเอาไปลบค่าคงที่ตรงๆ เช่น "... % 15) - 5"
    // ถ้าผลลัพธ์ modulo น้อยกว่าตัวลบ (เช่น modulo ได้ 0-4 แล้วลบ 5)
    // C++ จะคำนวณทั้งนิพจน์ด้วย unsigned arithmetic → underflow กลาย
    // เป็นเลขมหาศาลระดับ ~4.29 พันล้าน ก่อนถูก cast เป็น float แล้วเอาไป
    // บวก/คูณต่อ ทำให้ค่าที่ได้ "บวม" ผิดปกติ แก้โดย cast ผลลัพธ์ modulo
    // ให้เป็น signed (long) ก่อนลบเสมอ เพื่อบังคับให้คำนวณด้วย signed
    // arithmetic แทน (ค่าติดลบได้ตามปกติ ไม่ wrap around)
    tele.coolant_temp = 85.0f + (float)((long)(mockTickCount % 15) - 5L);
    tele.fuel_level   = max(0.0f, tele.fuel_level - 0.01f);
    if (tele.fuel_level == 0) tele.fuel_level = 80.0f;
    tele.temperature  = 27.0f + (float)((long)(mockTickCount % 40) - 10L) * 0.1f;   // เชียงใหม่เย็นกว่ากรุงเทพฯ เล็กน้อย
    tele.humidity     = 55.0f + (float)((long)(mockTickCount % 200) - 100L) * 0.1f;

    // baseline IMU (เกือบนิ่ง) — deterministic wobble แทนสุ่ม จะถูก
    // override ถ้าอยู่ใน phase harsh event
    float wobble = sinf((float)tele.ts * 0.3f);
    tele.ax = wobble * 0.05f;
    tele.ay = wobble * 0.03f;
    tele.az = 1.0f + wobble * 0.01f;
    tele.event          = "";
    tele.event_severity = 0.0f;

    // ── [FIX] เดินวนรอบ phase ตามระยะเวลาที่กำหนดตายตัว (round-robin) ──
    mockPhaseTicksLeft--;
    if (mockPhaseTicksLeft <= 0) {
        mockPhase = (mockPhase + 1) % MOCK_PHASE_COUNT;
        mockPhaseTicksLeft = MOCK_PHASE_DURATION_SEC[mockPhase];
    }

    switch (mockPhase) {

        case MOCK_PHASE_HARSH_BRAKE:
            tele.speed         = max(0.0f, MOCK_BASE_SPEED_KMH - 15.0f);
            tele.rpm           = 900 + (int)(tele.speed * 22);
            tele.throttle      = 10.0f;
            tele.engine_load   = 40.0f;
            tele.maf           = 6.0f;
            tele.event         = "harsh_brake";
            tele.event_severity = 82.0f;
            tele.ax            = -0.65f;
            mockAdvancePosition(tele.speed);
            break;

        case MOCK_PHASE_HARSH_ACCEL:
            tele.speed         = MOCK_BASE_SPEED_KMH + 10.0f;
            tele.rpm           = 900 + (int)(tele.speed * 22);
            tele.throttle      = 80.0f;
            tele.engine_load   = 85.0f;
            tele.maf           = 12.0f;
            tele.event         = "harsh_acceleration";
            tele.event_severity = 78.0f;
            tele.ax            = 0.62f;
            mockAdvancePosition(tele.speed);
            break;

        case MOCK_PHASE_HARSH_CORNER:
            tele.speed         = MOCK_BASE_SPEED_KMH * 0.7f;
            tele.rpm           = 900 + (int)(tele.speed * 22);
            tele.throttle      = 35.0f;
            tele.engine_load   = 55.0f;
            tele.maf           = 8.0f;
            tele.event         = "harsh_cornering";
            tele.event_severity = 74.0f;
            tele.ay            = 0.58f;
            mockAdvancePosition(tele.speed);
            break;

        case MOCK_PHASE_BUMP:
            tele.speed         = MOCK_BASE_SPEED_KMH;
            tele.rpm           = 900 + (int)(tele.speed * 22);
            tele.throttle      = 30.0f;
            tele.engine_load   = 50.0f;
            tele.maf           = 8.5f;
            tele.event         = "bump";
            tele.event_severity = 70.0f;
            tele.az            = 3.6f;
            mockAdvancePosition(tele.speed);
            break;

        case MOCK_PHASE_SPEEDING:
            // [FIX] threshold ตายตัว = 90 km/h ตามที่กำหนด, ไม่มีการสุ่มค่า
            tele.speed          = MOCK_SPEEDING_KMH + 10.0f;   // 100 km/h คงที่
            tele.rpm            = 900 + (int)(tele.speed * 22);
            tele.throttle       = 90.0f;
            tele.engine_load    = 88.0f;
            tele.maf            = 14.0f;
            tele.event          = "speeding";
            tele.event_severity = 90.0f;
            mockAdvancePosition(tele.speed);
            break;

        case MOCK_PHASE_IDLING:
            tele.speed         = 0.0f;
            tele.rpm           = 800;
            tele.throttle      = 0.0f;
            tele.engine_load   = 12.0f;
            tele.maf           = 1.2f;
            tele.event         = "idling";
            tele.event_severity = 100.0f;
            // ไม่เรียก mockAdvancePosition() — รถจอดอยู่กับที่จริงๆ ระหว่าง idle
            break;

        default: // NORMAL_1..6 — ขับปกติ
            tele.speed         = MOCK_BASE_SPEED_KMH;
            tele.rpm           = 900 + (int)(tele.speed * 22);
            tele.throttle      = 30.0f;
            tele.engine_load   = 50.0f;
            tele.maf           = 8.5f;
            mockAdvancePosition(tele.speed);
            break;
    }

    // ── Ignition cycle: เรียกท้ายสุดเสมอ (override เมื่อดับเครื่อง) ─
    bool justSwitched = updateMockIgnitionCycle();

    if (justSwitched) {
        Serial.printf(
            "[Mock] device=%s ignition %s (ts=%lu)\n",
            creds.device_id,
            tele.ignition ? "ON — trip start" : "OFF — trip closing (debounce 30s ที่ backend)",
            tele.ts);
    }
}
#endif

// =============================================
//  BUILD + SIGN PAYLOAD
// =============================================
String buildPayload() {
    JsonDocument doc;
    doc["device_id"]      = tele.device_id;
    doc["device_name"]    = tele.device_name;
    doc["ts"]             = tele.ts;
    doc["lat"]            = serialized(String(tele.lat, 7));
    doc["lon"]            = serialized(String(tele.lon, 7));
    doc["speed"]          = tele.speed;
    doc["heading"]        = tele.heading;
    doc["alt"]            = tele.altitude;
    doc["hdop"]           = tele.hdop;
    doc["rpm"]            = tele.rpm;
    doc["throttle"]       = tele.throttle;
    doc["engine_load"]    = tele.engine_load;
    doc["coolant_temp"]   = tele.coolant_temp;
    doc["fuel_level"]     = tele.fuel_level;
    doc["maf"]            = tele.maf;
    doc["ax"]             = tele.ax;
    doc["ay"]             = tele.ay;
    doc["az"]             = tele.az;
    doc["gx"]             = tele.gx;
    doc["gy"]             = tele.gy;
    doc["gz"]             = tele.gz;
    doc["event"]          = tele.event;
    doc["event_severity"] = tele.event_severity;
    doc["ignition"]       = tele.ignition;
    doc["temperature"]    = tele.temperature;
    doc["humidity"]       = tele.humidity;
    String raw;
    serializeJson(doc, raw);

    // Sign ด้วย HMAC-SHA256
    return buildSignedPayload(raw, creds.hmac_secret);
}

// =============================================
//  MQTT CALLBACK
// =============================================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
    String msg;
    for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
    Serial.printf("[MQTT] Received: %s\n", msg.c_str());

    JsonDocument doc;
    if (!deserializeJson(doc, msg)) {
        if (doc["device_name"].is<const char*>()) {
            String newName = doc["device_name"].as<String>();
            tele.device_name = newName;
            // บันทึกกลับ NVS ด้วย
            nvs_handle_t h;
            if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
                nvs_set_str(h, NVS_KEY_DEVICE_NAME, newName.c_str());
                nvs_commit(h);
                nvs_close(h);
            }
            displayDirty = true;
        }
    }
}

// =============================================
//  WiFi CONNECT
// =============================================
bool connectWiFi() {
    Serial.printf("[WiFi] Connecting to %s...\n", creds.wifi_ssid);
    WiFi.mode(WIFI_STA);
    WiFi.begin(creds.wifi_ssid, creds.wifi_pass);
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start > WIFI_TIMEOUT_MS) {
            Serial.println("[WiFi] Timeout!");
            return false;
        }
        delay(300);
    }
    Serial.printf("[WiFi] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
}

// =============================================
//  MQTT CONNECT (plain -- ไม่ใช้ TLS)
// =============================================
bool connectMQTT() {
    Serial.printf("[MQTT] Connecting to %s...\n", creds.mqtt_host);

    // [FIX] ไม่ต้อง setInsecure()/setCACert() แล้ว -- WiFiClient ธรรมดา
    // ไม่ทำ TLS handshake อยู่แล้ว ตรงกับ broker local ที่เปิดพอร์ต
    // 1884 แบบ plain MQTT (docker-compose: "1884:1883", ไม่มี TLS listener)

    mqttClient.setServer(MQTT_HOST, MQTT_PORT);
    mqttClient.setCallback(mqttCallback);
    mqttClient.setBufferSize(1024);
    mqttClient.setKeepAlive(60);

    if (mqttClient.connect(MQTT_CLIENT_ID, creds.mqtt_user, creds.mqtt_pass)) {
        Serial.println("[MQTT] Connected (plain, local broker)!");
        mqttClient.subscribe(TOPIC_CONFIG, MQTT_QOS);

        // Publish online status
        char status[128];
        snprintf(status, sizeof(status),
            "{\"status\":\"online\",\"device_id\":\"%s\"}", creds.device_id);
        mqttClient.publish(TOPIC_STATUS, status, true);
        return true;
    }
    Serial.printf("[MQTT] Failed rc=%d\n", mqttClient.state());
    return false;
}

// =============================================
//  DRAW HELPERS
// =============================================
void drawCard(int x, int y, int w, int h, uint16_t color = CLR_CARD) {
    gfx->fillRoundRect(x, y, w, h, 8, color);
}
void drawBar(int x, int y, int w, int h, int pct, uint16_t c) {
    gfx->fillRoundRect(x, y, w, h, h/2, CLR_BG);
    int fill = constrain((w * pct) / 100, 0, w);
    if (fill > 0) gfx->fillRoundRect(x, y, fill, h, h/2, c);
}
void txt(int x, int y, const char* s, uint16_t c = CLR_WHITE, uint8_t sz = 1) {
    gfx->setTextColor(c); gfx->setTextSize(sz);
    gfx->setCursor(x, y); gfx->print(s);
}
void txtFloat(int x, int y, float v, int dec, uint16_t c = CLR_WHITE, uint8_t sz = 1) {
    gfx->setTextColor(c); gfx->setTextSize(sz);
    gfx->setCursor(x, y); gfx->print(v, dec);
}

// [FIX] เดิมเมื่อค่าเซนเซอร์เป็น NaN (ยังไม่ได้ถูก assign จริง) จะ fallback
// เป็นเลข "0" ซึ่งทำให้ผู้ใช้เข้าใจผิดว่าค่าจริงคือ 0 (เช่น "0" โผล่ก่อน
// คำว่า Celsius/Percent ราวกับเซนเซอร์วัดได้ 0 องศา/0%) — เปลี่ยนเป็น
// "--" แทน เพื่อสื่อว่ายังไม่มีค่าจริง ไม่ใช่ 0 จริง
String dispInt(float v, float lo, float hi) {
    if (isnan(v)) return "--";
    return String((int)constrain(v, lo, hi));
}

// =============================================
//  DRAW DASHBOARD
// =============================================
void drawDashboard() {
    gfx->fillScreen(CLR_BG);

    // Header
    gfx->fillRect(0, 0, SCR_W, 42, CLR_ACCENT);
    txt(10, 6,  "FLEET TRACKER", 0x001F, 1);
    txt(10, 18, tele.device_name.c_str(), 0x001F, 1);
    txt(10, 30, creds.device_id, 0x001F, 1);

    // Security indicator
    gfx->fillRect(SCR_W-20, 2, 18, 18, CLR_GREEN);
    txt(SCR_W-16, 6, "S", 0x001F, 1);

    // WiFi + MQTT dots
    uint16_t wColor = (WiFi.status() == WL_CONNECTED) ? CLR_GREEN : CLR_RED;
    uint16_t mColor = mqttClient.connected() ? CLR_GREEN : CLR_RED;

    // วาดวงกลม
    gfx->fillCircle(SCR_W-45, 14, 5, wColor);
    gfx->fillCircle(SCR_W-30, 14, 5, mColor);

    // วาดตัวอักษรแยก
    gfx->setTextColor(CLR_WHITE);
    gfx->setTextSize(1);
    gfx->setCursor(SCR_W-48, 20);
    gfx->print("W");
    gfx->setCursor(SCR_W-33, 20);
    gfx->print("M");

    // Card 1: Status + Speed
    drawCard(8, 50, SCR_W-16, 72);
    txt(18, 58, "STATUS", CLR_GRAY);
    gfx->fillCircle(115, 62, 6, tele.ignition ? CLR_GREEN : CLR_RED);
    txt(124, 56, tele.ignition ? "ENGINE ON" : "ENGINE OFF",
        tele.ignition ? CLR_GREEN : CLR_RED);
    gfx->setTextColor(CLR_WHITE); gfx->setTextSize(3);
    gfx->setCursor(18, 72); gfx->print((int)tele.speed);
    txt(62, 88, "km/h", CLR_GRAY);
    txt(130, 68, "RPM", CLR_GRAY);
    gfx->setTextColor(CLR_ACCENT); gfx->setTextSize(2);
    gfx->setCursor(130, 80); gfx->print(tele.rpm);
    if (tele.event != "") {
        gfx->fillRoundRect(200, 60, 108, 24, 6, CLR_RED);
        txt(206, 66, "!", CLR_WHITE, 2);
        txt(222, 68, tele.event.c_str(), CLR_WHITE, 1);
    }

    // Card 2: GPS
    drawCard(8, 130, SCR_W-16, 62);
    txt(18, 138, "GPS", CLR_GRAY);
    char latStr[16], lonStr[16];
    snprintf(latStr, sizeof(latStr), "%.5f", tele.lat);
    snprintf(lonStr, sizeof(lonStr), "%.5f", tele.lon);
    txt(18, 150, latStr, CLR_WHITE);
    txt(100, 150, lonStr, CLR_ACCENT);
    txt(200, 138, "HDOP");
    txtFloat(200, 150, tele.hdop, 1, tele.hdop < 2.0 ? CLR_GREEN : CLR_RED);
    txt(250, 138, "HDG", CLR_GRAY);
    gfx->setTextColor(CLR_WHITE); gfx->setCursor(250, 150);
    gfx->print(tele.heading);

    // Card 3: OBD2
    drawCard(8, 200, SCR_W-16, 90);
    txt(18, 208, "OBD2 / ENGINE", CLR_GRAY);
    txt(18, 220, "THROTTLE", CLR_GRAY);
    drawBar(18, 230, 130, 8, (int)tele.throttle, CLR_ACCENT);
    txt(155, 222, (String((int)tele.throttle)+"%").c_str(), CLR_WHITE);
    txt(18, 245, "LOAD", CLR_GRAY);
    drawBar(18, 255, 130, 8, (int)tele.engine_load, CLR_TEAL);
    txt(155, 247, (String((int)tele.engine_load)+"%").c_str(), CLR_WHITE);
    txt(200, 208, "COOLANT", CLR_GRAY);
    gfx->setTextColor(tele.coolant_temp > 100 ? CLR_RED : CLR_WHITE);
    gfx->setTextSize(2); gfx->setCursor(200, 220);
    // [FIX] ดูคำอธิบายเดียวกับบอร์ด KTC-111 — clamp ก่อน cast ป้องกัน
    // (int)NaN/garbage-float กลายเป็น INT32_MAX บนจอ และใช้ dispInt()
    // เพื่อแสดง "--" แทน "0" เมื่อค่ายังไม่ valid (ไม่ให้ดูเหมือนมีเลข 0
    // โผล่ก่อนหน่วย "C")
    gfx->print(dispInt(tele.coolant_temp, -50.0f, 200.0f));
    txt(232, 228, "C", CLR_GRAY);
    txt(200, 248, "MAF", CLR_GRAY);
    txtFloat(200, 258, tele.maf, 1, CLR_WHITE);
    txt(18, 272, "FUEL", CLR_GRAY);
    uint16_t fuelColor = tele.fuel_level < 20 ? CLR_RED :
                         tele.fuel_level < 40 ? CLR_ORANGE : CLR_GREEN;
    drawBar(18, 281, 200, 10, (int)tele.fuel_level, fuelColor);
    txt(225, 275, (String((int)tele.fuel_level)+"%").c_str(), fuelColor);

    // Card 4: Sensor
    drawCard(8, 300, 148, 70);
    txt(18, 308, "TEMP", CLR_GRAY);
    uint16_t tempColor = tele.temperature > 35 ? CLR_RED :
                         tele.temperature > 30 ? CLR_ORANGE : CLR_WHITE;
    gfx->setTextColor(tempColor); gfx->setTextSize(3);
    gfx->setCursor(18, 320);
    // [FIX] เดิมพิมพ์ tele.temperature ตรงๆ โดยไม่มี guard เลย — ถ้าค่า
    // เป็น NaN (เช่น ก่อน updateMockData() รอบแรก) หรือหลุด clamp เพราะ
    // บั๊ก unsigned-underflow ด้านบน จะโชว์ "nan" หรือเลขบวมผิดปกติทับ
    // คำว่า "Celsius" ด้านล่าง — แก้ให้ clamp ช่วงที่สมเหตุสมผลของอากาศ
    // ประเทศไทย (-10°C ถึง 60°C) และโชว์ "--" แทนถ้ายังไม่ valid
    if (isnan(tele.temperature)) {
        gfx->print("--");
    } else {
        gfx->print(constrain(tele.temperature, -10.0f, 60.0f), 1);
    }
    txt(18, 352, "Celsius", CLR_GRAY);
    drawCard(164, 300, 148, 70);
    txt(174, 308, "HUMIDITY", CLR_GRAY);
    gfx->setTextColor(CLR_ACCENT); gfx->setTextSize(3);
    // [FIX] เหตุผลเดียวกัน — clamp ก่อน cast ป้องกันค่าประหลาดบนจอ และ
    // ใช้ dispInt() แสดง "--" แทน "0" เมื่อยังไม่มีค่าจริง (ไม่ให้ดูเหมือน
    // มีเลข 0 โผล่ก่อนคำว่า "Percent")
    gfx->setCursor(174, 320); gfx->print(dispInt(tele.humidity, 0.0f, 100.0f));
    txt(174, 352, "Percent", CLR_GRAY);

    // Footer: TX count + security status
    drawCard(8, 380, SCR_W-16, 36);
    txt(18, 388, "TX:", CLR_GRAY);
    txt(38, 388, String(publishCount).c_str(), CLR_GREEN);
    txt(80, 388, "| HMAC-SHA256 signed", CLR_TEAL);
    txt(18, 402, "MQTT: plain (no TLS)", CLR_YELLOW);

    // Button
    uint16_t btnColor = mqttClient.connected() ? CLR_ACCENT : CLR_GRAY;
    gfx->fillRoundRect(8, 424, SCR_W-16, 46, 10, btnColor);
    txt(mqttClient.connected() ? 80 : 65, 438,
        mqttClient.connected() ? "SEND NOW" : "CONNECTING...",
        CLR_WHITE, 2);

    gfx->flush();
    displayDirty = false;
}

// =============================================
//  TOUCH
// =============================================
bool readTouch(int &tx, int &ty) {
    Wire.beginTransmission(TOUCH_ADDR);
    Wire.write(0x00);
    if (Wire.endTransmission(false) != 0) return false;
    Wire.requestFrom(TOUCH_ADDR, 7);
    if (Wire.available() < 7) return false;
    Wire.read();
    uint8_t fingers = Wire.read() & 0x0F;
    if (fingers == 0) return false;
    Wire.read();
    uint8_t xh = Wire.read(), xl = Wire.read();
    uint8_t yh = Wire.read(), yl = Wire.read();
    tx = ((xh & 0x0F) << 8) | xl;
    ty = ((yh & 0x0F) << 8) | yl;
    return true;
}

// =============================================
//  SETUP
// =============================================
void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("\n[Boot] Fleet Telematics ESP32-S3");

    // Init NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    // Provisioning mode (รันครั้งแรกครั้งเดียว)
#ifdef PROVISION_MODE
    provisionNVS();
    Serial.println("[Boot] Provision complete. Please reflash without PROVISION_MODE");
    while(1) delay(1000);
#endif

    // โหลด credentials จาก NVS
    if (!nvsLoadCredentials()) {
        Serial.println("[Boot] Using hardcoded fallback — please provision NVS!");
        // Fallback ถ้า NVS ว่าง (ยังไม่ได้ provision)
        strncpy(creds.wifi_ssid,   "Tateev",        sizeof(creds.wifi_ssid));
        strncpy(creds.wifi_pass,   "12345678",       sizeof(creds.wifi_pass));
        strncpy(creds.mqtt_host,   "192.168.1.37",   sizeof(creds.mqtt_host));
        strncpy(creds.mqtt_user,   "abktcs",         sizeof(creds.mqtt_user));
        strncpy(creds.mqtt_pass,   "Ab12345678",     sizeof(creds.mqtt_pass));
        strncpy(creds.hmac_secret, "fleet_hmac_secret_KTC001_2026", sizeof(creds.hmac_secret));
        strncpy(creds.device_id,   "KTC-222",        sizeof(creds.device_id));
        strncpy(creds.device_name, "Car-KTCS-222-CNX", sizeof(creds.device_name));
    }

    // Set telemetry identity จาก NVS
    tele.device_id   = String(creds.device_id);
    tele.device_name = String(creds.device_name);

    // สร้าง MQTT topics จาก device_id
    snprintf(TOPIC_TELEMETRY, sizeof(TOPIC_TELEMETRY),
             "kotchasaan/fleet/%s/telemetry", creds.device_id);
    snprintf(TOPIC_STATUS, sizeof(TOPIC_STATUS),
             "kotchasaan/fleet/%s/status", creds.device_id);
    snprintf(TOPIC_CONFIG, sizeof(TOPIC_CONFIG),
             "kotchasaan/fleet/%s/config", creds.device_id);
    snprintf(MQTT_CLIENT_ID, sizeof(MQTT_CLIENT_ID),
             "esp32-%s", creds.device_id);

    // Display init
    Wire.begin(TOUCH_SDA, TOUCH_SCL);
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, HIGH);
    gfx->begin();
    gfx->setRotation(0);

    // Splash
    gfx->fillScreen(0x0000);
    gfx->setTextColor(CLR_ACCENT); gfx->setTextSize(2);
    gfx->setCursor(30, 180); gfx->print("FLEET TELEMATICS");
    gfx->setTextColor(CLR_WHITE); gfx->setTextSize(1);
    gfx->setCursor(60, 210); gfx->print(creds.device_id);
    gfx->setTextColor(CLR_YELLOW);
    gfx->setCursor(60, 240); gfx->print("Plain MQTT + HMAC Security");
    gfx->flush();
    delay(1500);

    tele.fuel_level = 75.0;

    // [FIX] เรียก updateMockData() ก่อน drawDashboard() ครั้งแรกเสมอ —
    // เดิม drawDashboard() ถูกเรียกก่อน coolant_temp/humidity ถูก assign
    // ค่าจริงเลย (updateMockData() อยู่ใน loop() เท่านั้น) ทำให้เฟรมแรก
    // แสดงค่า default/garbage แทน
#ifdef ENABLE_MOCK_DATA
    updateMockData();
#else
    tele.coolant_temp = 80.0f;
    tele.temperature  = 27.0f;
    tele.humidity     = 55.0f;
#endif

    connectWiFi();
    if (WiFi.status() == WL_CONNECTED) connectMQTT();

    drawDashboard();
}

// =============================================
//  LOOP
// =============================================
void loop() {
    // Reconnect WiFi
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WiFi] Reconnecting...");
        connectWiFi();
    }

    // Reconnect MQTT
    if (!mqttClient.connected()) {
        Serial.println("[MQTT] Reconnecting...");
        delay(MQTT_RECONNECT_DELAY_MS);
        connectMQTT();
        displayDirty = true;
    }
    mqttClient.loop();

#ifdef ENABLE_MOCK_DATA
    updateMockData();
    displayDirty = true;
#endif

    // Publish signed payload
    if (mqttClient.connected() &&
        millis() - lastPublish >= MQTT_PUBLISH_INTERVAL_MS) {
        String payload = buildPayload();
        bool ok = mqttClient.publish(TOPIC_TELEMETRY, payload.c_str(), false);
        if (ok) {
            publishCount++;
            Serial.printf("[MQTT] Published #%lu (signed)\n", publishCount);
        }
        lastPublish = millis();
    }

    if (displayDirty) drawDashboard();

    int tx, ty;
    if (readTouch(tx, ty)) {
        if (ty > 424) lastPublish = 0;
    }

    delay(20);
}