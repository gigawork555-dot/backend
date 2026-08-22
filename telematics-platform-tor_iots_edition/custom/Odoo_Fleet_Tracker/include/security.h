#pragma once
#include <Arduino.h>
#include <nvs_flash.h>
#include <nvs.h>
#include <mbedtls/md.h>
#include <mbedtls/base64.h>

// =============================================
//  NVS NAMESPACE
// =============================================
#define NVS_NAMESPACE   "fleet_cfg"
#define NVS_KEY_WIFI_SSID     "wifi_ssid"
#define NVS_KEY_WIFI_PASS     "wifi_pass"
#define NVS_KEY_MQTT_HOST     "mqtt_host"
#define NVS_KEY_MQTT_USER     "mqtt_user"
#define NVS_KEY_MQTT_PASS     "mqtt_pass"
#define NVS_KEY_HMAC_SECRET   "hmac_secret"
#define NVS_KEY_DEVICE_ID     "device_id"
#define NVS_KEY_DEVICE_NAME   "device_name"

// =============================================
//  RUNTIME CREDENTIALS (โหลดจาก NVS ตอน boot)
// =============================================
struct Credentials {
    char wifi_ssid[64]    = {0};
    char wifi_pass[64]    = {0};
    char mqtt_host[128]   = {0};
    char mqtt_user[64]    = {0};
    char mqtt_pass[64]    = {0};
    char hmac_secret[64]  = {0};
    char device_id[32]    = {0};
    char device_name[64]  = {0};
    bool loaded           = false;
};

Credentials creds;

// =============================================
//  NVS: เขียน credentials (รันครั้งแรกครั้งเดียว)
// =============================================
bool nvsWriteCredentials(
    const char* wifi_ssid,
    const char* wifi_pass,
    const char* mqtt_host,
    const char* mqtt_user,
    const char* mqtt_pass,
    const char* hmac_secret,
    const char* device_id,
    const char* device_name
) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        Serial.printf("[NVS] Open failed: %s\n", esp_err_to_name(err));
        return false;
    }

    nvs_set_str(handle, NVS_KEY_WIFI_SSID,   wifi_ssid);
    nvs_set_str(handle, NVS_KEY_WIFI_PASS,   wifi_pass);
    nvs_set_str(handle, NVS_KEY_MQTT_HOST,   mqtt_host);
    nvs_set_str(handle, NVS_KEY_MQTT_USER,   mqtt_user);
    nvs_set_str(handle, NVS_KEY_MQTT_PASS,   mqtt_pass);
    nvs_set_str(handle, NVS_KEY_HMAC_SECRET, hmac_secret);
    nvs_set_str(handle, NVS_KEY_DEVICE_ID,   device_id);
    nvs_set_str(handle, NVS_KEY_DEVICE_NAME, device_name);

    err = nvs_commit(handle);
    nvs_close(handle);

    if (err == ESP_OK) {
        Serial.println("[NVS] Credentials saved successfully");
        return true;
    }
    Serial.printf("[NVS] Commit failed: %s\n", esp_err_to_name(err));
    return false;
}

// =============================================
//  NVS: อ่าน credentials ตอน boot
// =============================================
bool nvsLoadCredentials() {
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        Serial.println("[NVS] No credentials found, using defaults from config.h");
        return false;
    }

    size_t len;
    auto readStr = [&](const char* key, char* buf, size_t bufSize) {
        len = bufSize;
        esp_err_t e = nvs_get_str(handle, key, buf, &len);
        if (e != ESP_OK) Serial.printf("[NVS] Key '%s' not found\n", key);
        return e == ESP_OK;
    };

    bool ok = true;
    ok &= readStr(NVS_KEY_WIFI_SSID,   creds.wifi_ssid,   sizeof(creds.wifi_ssid));
    ok &= readStr(NVS_KEY_WIFI_PASS,   creds.wifi_pass,   sizeof(creds.wifi_pass));
    ok &= readStr(NVS_KEY_MQTT_HOST,   creds.mqtt_host,   sizeof(creds.mqtt_host));
    ok &= readStr(NVS_KEY_MQTT_USER,   creds.mqtt_user,   sizeof(creds.mqtt_user));
    ok &= readStr(NVS_KEY_MQTT_PASS,   creds.mqtt_pass,   sizeof(creds.mqtt_pass));
    ok &= readStr(NVS_KEY_HMAC_SECRET, creds.hmac_secret, sizeof(creds.hmac_secret));
    ok &= readStr(NVS_KEY_DEVICE_ID,   creds.device_id,   sizeof(creds.device_id));
    ok &= readStr(NVS_KEY_DEVICE_NAME, creds.device_name, sizeof(creds.device_name));

    nvs_close(handle);
    creds.loaded = ok;

    if (ok) Serial.println("[NVS] Credentials loaded OK");
    return ok;
}

// =============================================
//  HMAC-SHA256 Signing
// =============================================
String hmacSign(const String& payload, const char* secret) {
    uint8_t hmac[32];
    mbedtls_md_context_t ctx;
    mbedtls_md_init(&ctx);
    mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
    mbedtls_md_hmac_starts(&ctx,
        (const uint8_t*)secret, strlen(secret));
    mbedtls_md_hmac_update(&ctx,
        (const uint8_t*)payload.c_str(), payload.length());
    mbedtls_md_hmac_finish(&ctx, hmac);
    mbedtls_md_free(&ctx);

    // แปลงเป็น hex string
    String sig = "";
    for (int i = 0; i < 32; i++) {
        char hex[3];
        snprintf(hex, sizeof(hex), "%02x", hmac[i]);
        sig += hex;
    }
    return sig;
}

// =============================================
//  สร้าง signed payload
//  เพิ่ม field "sig" เข้าไปใน JSON
// =============================================
String buildSignedPayload(const String& payload, const char* secret) {
    String sig = hmacSign(payload, secret);

    // ใส่ signature เข้าไปใน JSON
    // payload = {...} → {...,"sig":"abcd1234..."}
    String signed_payload = payload;
    signed_payload.remove(signed_payload.length() - 1); // ลบ }
    signed_payload += ",\"sig\":\"" + sig + "\"}";
    return signed_payload;
}