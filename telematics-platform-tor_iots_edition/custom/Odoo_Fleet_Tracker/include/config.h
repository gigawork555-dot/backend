#pragma once

// ===================================================
//  DEVICE IDENTITY — แก้ต่อบอร์ด
// ===================================================
#define DEVICE_ID       "KTC-222"
#define DEVICE_NAME     "Car-KTCS-222"

// ===================================================
//  WiFi
// ===================================================
#define WIFI_SSID       "KOTCHASAAN@LIVE_2.4G"
#define WIFI_PASSWORD   "T626Pro1"
#define WIFI_TIMEOUT_MS 15000

// ===================================================
//  EMQX Broker (Local Docker)
//  ใส่ IP ของเครื่อง Windows ที่รัน Docker
//  หาได้จาก: ipconfig → IPv4 Address
// ===================================================
#define MQTT_HOST       "192.168.1.48"
#define MQTT_PORT       1884  //1884แบบไม่มี tls
#define MQTT_USER       "admin"
#define MQTT_PASSWORD   "admin123"
// #define MQTT_CLIENT_ID  "esp32-" DEVICE_ID

// ===================================================
//  HMAC Secret — ต้องตรงกับ docker-compose.yml
// ===================================================
#define HMAC_SECRET     "fleet_hmac_secret_KTC001_2026"

// ===================================================
//  MQTT Topics
// ===================================================
#define MQTT_TOPIC_TELEMETRY  "kotchasaan/fleet/" DEVICE_ID "/telemetry"
#define MQTT_TOPIC_STATUS     "kotchasaan/fleet/" DEVICE_ID "/status"
#define MQTT_TOPIC_CONFIG     "kotchasaan/fleet/" DEVICE_ID "/config"

// ===================================================
//  Timing
// ===================================================
#define MQTT_PUBLISH_INTERVAL_MS  5000
#define MQTT_RECONNECT_DELAY_MS   3000
#define MQTT_QOS                  1

// ===================================================
//  Display (JC3248W535C)
// ===================================================
#define TFT_CS    45
#define TFT_SCK   47
#define TFT_D0    21
#define TFT_D1    48
#define TFT_D2    40
#define TFT_D3    39
#define TFT_RST   GFX_NOT_DEFINED
#define TFT_BL    1
#define SCR_W     320
#define SCR_H     480

// ===================================================
//  Touch
// ===================================================
#define TOUCH_SDA   4
#define TOUCH_SCL   8
#define TOUCH_ADDR  0x3B

// ===================================================
//  Mock Data
// ===================================================
#define ENABLE_MOCK_DATA