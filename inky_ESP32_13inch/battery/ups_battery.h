#pragma once

#include <Arduino.h>

struct BatteryTelemetry {
    bool available = false;
    uint8_t i2cAddress = 0;
    float busVoltageV = 0.0f;
    float shuntVoltageMv = 0.0f;
    float currentMa = 0.0f;
    float powerMw = 0.0f;
    int socPct = -1;
    char error[48] = {};
};

bool batteryBegin();
bool batteryRead(BatteryTelemetry *out);
void batteryPrint(const BatteryTelemetry &sample);
BatteryTelemetry batteryGetCached();
