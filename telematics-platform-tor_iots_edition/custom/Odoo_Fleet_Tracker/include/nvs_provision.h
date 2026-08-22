#pragma once
// =============================================
//  NVS PROVISIONING
//  รันครั้งเดียวตอน first boot เพื่อเขียน
//  credentials ลง NVS flash
//  หลังจากนั้น comment #define PROVISION_MODE
// =============================================

// #define PROVISION_MODE   // ← uncomment เพื่อเขียน NVS ครั้งแรก

#ifdef PROVISION_MODE
void provisionNVS() {
    Serial.println("[NVS] PROVISION MODE - Writing credentials...");
    
    bool ok = nvsWriteCredentials(
        "KOTCHASAAN@LIVE_2.4G",           // WiFi SSID
        "T626Pro1",         // WiFi Password
        "192.168.1.48", // MQTT Host
        "admin",           // MQTT User
        "admin123",       // MQTT Password
        "fleet_hmac_secret_KTC001_2026",  // HMAC Secret (เปลี่ยนเป็นค่าสุ่มของคุณ)
        "KTC-222",          // Device ID
        "Car-KTCS-222"       // Device Name
    );
    
    if (ok) {
        Serial.println("[NVS] Provisioning complete!");
        Serial.println("[NVS] Please comment out #define PROVISION_MODE and reflash");
    } else {
        Serial.println("[NVS] Provisioning FAILED!");
    }
}
#endif