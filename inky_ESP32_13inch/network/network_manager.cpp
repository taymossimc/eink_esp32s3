#include "network_manager.h"

#include "config/device_config.h"
#include "power/power.h"

#include <ArduinoHttpClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <string.h>
#include <time.h>

namespace {

bool gStarted = false;
bool gTimeSynced = false;
SemaphoreHandle_t gNetworkMutex = nullptr;

class NetworkLock {
  public:
    NetworkLock() {
        if (!gNetworkMutex) gNetworkMutex = xSemaphoreCreateRecursiveMutex();
        locked_ = gNetworkMutex &&
                  xSemaphoreTakeRecursive(gNetworkMutex, portMAX_DELAY) == pdTRUE;
    }
    ~NetworkLock() {
        if (locked_) xSemaphoreGiveRecursive(gNetworkMutex);
    }
  private:
    bool locked_ = false;
};

const device_config::WifiProfile *findProfile(
    const device_config::DeviceConfig &config, const String &ssid) {
    for (size_t i = 0; i < device_config::kMaxWifiProfiles; ++i) {
        const auto &profile = config.wifiProfiles[i];
        if (profile.enabled && ssid == profile.ssid) return &profile;
    }
    return nullptr;
}

void syncNetworkTime() {
    if (gTimeSynced || WiFi.status() != WL_CONNECTED) return;
    if (time(nullptr) >= 1700000000) {
        gTimeSynced = true;
        return;
    }
    configTime(0, 0, "time.cloudflare.com", "pool.ntp.org");
    const uint32_t started = millis();
    while (time(nullptr) < 1700000000 && millis() - started < 10000) {
        delay(100);
    }
    gTimeSynced = time(nullptr) >= 1700000000;
    Serial.printf("WiFi: clock sync %s\n", gTimeSynced ? "ok" : "timed out");
}

}  // namespace

bool networkManagerBegin() {
    NetworkLock lock;
    if (gStarted) return true;
    WiFi.persistent(false);
    WiFi.setAutoReconnect(true);
    WiFi.mode(WIFI_AP_STA);
    gStarted = true;
    return true;
}

bool networkConnectSaved(uint32_t timeoutMs) {
    NetworkLock lock;
    networkManagerBegin();
    const auto config = device_config::getSnapshot();
    if (!config.wifiEnabled) {
        networkDisconnectStation();
        return false;
    }
    if (WiFi.status() == WL_CONNECTED &&
        findProfile(config, WiFi.SSID())) {
        syncNetworkTime();
        return true;
    }

    const int found = WiFi.scanNetworks(false, true);
    int bestIndex = -1;
    int32_t bestRssi = INT32_MIN;
    const device_config::WifiProfile *bestProfile = nullptr;
    for (int i = 0; i < found; ++i) {
        const auto *profile = findProfile(config, WiFi.SSID(i));
        if (profile && WiFi.RSSI(i) > bestRssi) {
            bestIndex = i;
            bestRssi = WiFi.RSSI(i);
            bestProfile = profile;
        }
    }
    WiFi.scanDelete();
    if (bestIndex < 0 || !bestProfile) {
        Serial.println("WiFi: no configured network in range");
        return false;
    }

    Serial.printf("WiFi: connecting to %s\n", bestProfile->ssid);
    WiFi.begin(bestProfile->ssid, bestProfile->password);
    const uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
        delay(100);
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi: connection failed");
        WiFi.disconnect(false, false);
        return false;
    }
    Serial.printf("WiFi: connected %s, IP=%s RSSI=%ld\n",
                  WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(),
                  static_cast<long>(WiFi.RSSI()));
    syncNetworkTime();
    return true;
}

void networkDisconnectStation() {
    NetworkLock lock;
    if (gStarted) WiFi.disconnect(false, false);
}

bool networkWifiConnected() {
    return WiFi.status() == WL_CONNECTED;
}

String networkWifiSsid() {
    return networkWifiConnected() ? WiFi.SSID() : String();
}

String networkWifiIp() {
    return networkWifiConnected() ? WiFi.localIP().toString() : String();
}

int32_t networkWifiRssi() {
    return networkWifiConnected() ? WiFi.RSSI() : 0;
}

size_t networkScan(WifiNetworkInfo *results, size_t capacity) {
    NetworkLock lock;
    if (!results || !capacity) return 0;
    networkManagerBegin();
    const int found = WiFi.scanNetworks(false, true);
    size_t count = 0;
    for (int i = 0; i < found && count < capacity; ++i) {
        bool duplicate = false;
        for (size_t j = 0; j < count; ++j) {
            if (WiFi.SSID(i) == results[j].ssid) {
                duplicate = true;
                if (WiFi.RSSI(i) > results[j].rssi) {
                    results[j].rssi = WiFi.RSSI(i);
                }
                break;
            }
        }
        if (duplicate) continue;
        strncpy(results[count].ssid, WiFi.SSID(i).c_str(),
                sizeof(results[count].ssid) - 1);
        results[count].rssi = WiFi.RSSI(i);
        results[count].secured =
            WiFi.encryptionType(i) != WIFI_AUTH_OPEN;
        ++count;
    }
    WiFi.scanDelete();
    return count;
}

bool networkWifiHttpsRequest(
    const char *host, const char *requestPath, const char *method,
    const uint8_t *body, size_t bodyLen, const char *timestamp,
    const char *nonce, const char *signature, uint8_t **bodyOut,
    size_t *lenOut, size_t maxBytes, int *statusOut) {
    NetworkLock lock;
    if (!host || !requestPath || !method || !bodyOut || !lenOut || !maxBytes) {
        return false;
    }
    *bodyOut = nullptr;
    *lenOut = 0;
    if (statusOut) *statusOut = 0;
    if (!networkWifiConnected() && !networkConnectSaved()) return false;

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(30);
    HttpClient http(client, host, 443);
    http.setTimeout(120000);
    http.connectionKeepAlive();
    http.beginRequest();
    const int rc = http.startRequest(requestPath, method);
    if (rc != 0) {
        http.stop();
        return false;
    }
    if (timestamp && timestamp[0]) http.sendHeader("X-TMAds-Timestamp", timestamp);
    if (nonce && nonce[0]) http.sendHeader("X-TMAds-Nonce", nonce);
    if (signature && signature[0]) http.sendHeader("X-TMAds-Signature", signature);
    if (body && bodyLen) {
        http.sendHeader("Content-Type", "application/json");
        http.sendHeader("Content-Length", static_cast<int>(bodyLen));
        http.beginBody();
        if (http.write(body, bodyLen) != bodyLen) {
            http.stop();
            return false;
        }
    }
    http.endRequest();
    const int status = http.responseStatusCode();
    if (statusOut) *statusOut = status;
    if (status < 200 || status >= 300) {
        const String detail = http.responseBody();
        Serial.printf("WiFi HTTPS %s: HTTP %d %s\n", method, status,
                      detail.c_str());
        http.stop();
        return false;
    }
    const int declared = http.contentLength();
    if (declared > 0 && static_cast<size_t>(declared) > maxBytes) {
        http.stop();
        return false;
    }
    uint8_t *buffer = static_cast<uint8_t *>(
        heap_caps_malloc(maxBytes, MALLOC_CAP_SPIRAM));
    if (!buffer) buffer = static_cast<uint8_t *>(malloc(maxBytes));
    if (!buffer) {
        http.stop();
        return false;
    }
    size_t total = 0;
    uint32_t lastData = millis();
    while ((declared < 0 || total < static_cast<size_t>(declared)) &&
           total < maxBytes) {
        const size_t available = http.available();
        if (available) {
            const size_t request =
                min(available, min(static_cast<size_t>(8192), maxBytes - total));
            const int got = http.readBytes(buffer + total, request);
            if (got > 0) {
                total += static_cast<size_t>(got);
                lastData = millis();
            }
        } else if (!http.connected() || millis() - lastData > 30000) {
            break;
        } else {
            delay(10);
        }
    }
    http.stop();
    if (!total || (declared >= 0 && total != static_cast<size_t>(declared))) {
        heap_caps_free(buffer);
        return false;
    }
    *bodyOut = buffer;
    *lenOut = total;
    return true;
}

bool networkWifiHttpsGet(const char *host, const char *path,
                         uint8_t **bodyOut, size_t *lenOut,
                         size_t maxBytes) {
    NetworkLock lock;
    if (!host || !path || !bodyOut || !lenOut || !maxBytes) return false;
    *bodyOut = nullptr;
    *lenOut = 0;
    if (!networkWifiConnected() && !networkConnectSaved()) return false;

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(30);
    HttpClient http(client, host, 443);
    http.setTimeout(30000);
    if (http.get(path) != 0) return false;
    const int status = http.responseStatusCode();
    if (status != 200 && status != 206) {
        Serial.printf("WiFi HTTPS: HTTP %d\n", status);
        http.stop();
        return false;
    }
    const int declared = http.contentLength();
    if (declared > 0 && static_cast<size_t>(declared) > maxBytes) {
        Serial.println("WiFi HTTPS: response exceeds limit");
        http.stop();
        return false;
    }
    uint8_t *buffer = static_cast<uint8_t *>(
        heap_caps_malloc(maxBytes, MALLOC_CAP_SPIRAM));
    if (!buffer) buffer = static_cast<uint8_t *>(malloc(maxBytes));
    if (!buffer) {
        http.stop();
        return false;
    }

    size_t total = 0;
    uint32_t lastData = millis();
    while ((declared < 0 || total < static_cast<size_t>(declared)) &&
           total < maxBytes) {
        const size_t available = http.available();
        if (available) {
            const size_t request =
                min(available, min(static_cast<size_t>(8192), maxBytes - total));
            const int got = http.readBytes(buffer + total, request);
            if (got > 0) {
                total += static_cast<size_t>(got);
                lastData = millis();
                if ((total & 0xFFFFU) < static_cast<size_t>(got)) activityBump();
            }
        } else if (!http.connected() || millis() - lastData > 30000) {
            break;
        } else {
            delay(10);
        }
    }
    http.stop();
    if (!total || (declared >= 0 && total != static_cast<size_t>(declared))) {
        heap_caps_free(buffer);
        return false;
    }
    *bodyOut = buffer;
    *lenOut = total;
    return true;
}

bool networkWifiBandwidthTest(size_t bytes, float *mbps,
                              uint32_t *durationMs) {
    if (!mbps || !durationMs || bytes < 1024 || bytes > 5U * 1024U * 1024U) {
        return false;
    }
    char path[64] = {};
    snprintf(path, sizeof(path), "/__down?bytes=%u",
             static_cast<unsigned>(bytes));
    uint8_t *body = nullptr;
    size_t received = 0;
    const uint32_t start = millis();
    const bool ok = networkWifiHttpsGet("speed.cloudflare.com", path, &body,
                                        &received, bytes);
    *durationMs = millis() - start;
    if (body) heap_caps_free(body);
    if (!ok || !*durationMs) return false;
    *mbps = static_cast<float>(received) * 8.0f /
            (static_cast<float>(*durationMs) * 1000.0f);
    return true;
}
