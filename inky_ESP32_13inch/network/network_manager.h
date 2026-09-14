#pragma once

#include <Arduino.h>

struct WifiNetworkInfo {
    char ssid[33] = {};
    int32_t rssi = 0;
    bool secured = false;
};

bool networkManagerBegin();
bool networkConnectSaved(uint32_t timeoutMs = 15000);
void networkDisconnectStation();
bool networkWifiConnected();
String networkWifiSsid();
String networkWifiIp();
int32_t networkWifiRssi();

size_t networkScan(WifiNetworkInfo *results, size_t capacity);

bool networkWifiHttpsGet(const char *host, const char *path,
                         uint8_t **bodyOut, size_t *lenOut,
                         size_t maxBytes);

bool networkWifiBandwidthTest(size_t bytes, float *mbps,
                              uint32_t *durationMs);
