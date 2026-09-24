#include "ups_battery.h"

#include "ups_battery_config.h"

#include <Wire.h>
#include <cstring>

namespace {

constexpr float kMinPackVoltageV = 9.0f;
constexpr float kMaxPackVoltageV = 12.6f;
constexpr float kCurrentLsbMa = 0.1f;
constexpr float kPowerLsbMw = 2.0f;
constexpr uint16_t kCalibrationValue = 4096;
constexpr uint16_t kIna219Config =
    0x2000 | 0x1800 | 0x0180 | 0x0048 | 0x0007;

constexpr uint8_t kRegConfig = 0x00;
constexpr uint8_t kRegShuntVoltage = 0x01;
constexpr uint8_t kRegBusVoltage = 0x02;
constexpr uint8_t kRegPower = 0x03;
constexpr uint8_t kRegCurrent = 0x04;
constexpr uint8_t kRegCalibration = 0x05;

constexpr uint8_t kProbeAddresses[] = {
    UPS_BATTERY_I2C_ADDR, 0x40, 0x41, 0x44, 0x45,
};

TwoWire gUpsWire(1);
bool gReady = false;
uint8_t gAddress = 0;
BatteryTelemetry gCached;

int16_t readRegister16(uint8_t reg) {
    gUpsWire.beginTransmission(gAddress);
    gUpsWire.write(reg);
    if (gUpsWire.endTransmission(false) != 0) {
        return 0;
    }
    if (gUpsWire.requestFrom(static_cast<int>(gAddress), 2) != 2) {
        return 0;
    }
    const uint8_t msb = gUpsWire.read();
    const uint8_t lsb = gUpsWire.read();
    return static_cast<int16_t>((static_cast<uint16_t>(msb) << 8) | lsb);
}

bool writeRegister16(uint8_t reg, uint16_t value) {
    gUpsWire.beginTransmission(gAddress);
    gUpsWire.write(reg);
    gUpsWire.write(static_cast<uint8_t>((value >> 8) & 0xFF));
    gUpsWire.write(static_cast<uint8_t>(value & 0xFF));
    return gUpsWire.endTransmission(true) == 0;
}

bool probeAddress(uint8_t address) {
    gUpsWire.beginTransmission(address);
    return gUpsWire.endTransmission(true) == 0;
}

bool configureIna219() {
    if (!writeRegister16(kRegCalibration, kCalibrationValue)) {
        return false;
    }
    return writeRegister16(kRegConfig, kIna219Config);
}

int estimateSocPct(float voltageV) {
    if (voltageV <= 0.0f) {
        return -1;
    }
    const float span = kMaxPackVoltageV - kMinPackVoltageV;
    const float ratio = (voltageV - kMinPackVoltageV) / span;
    int pct = static_cast<int>(ratio * 100.0f + 0.5f);
    if (pct < 0) {
        pct = 0;
    } else if (pct > 100) {
        pct = 100;
    }
    return pct;
}

void fillUnavailable(BatteryTelemetry *out, const char *message) {
    if (out == nullptr) {
        return;
    }
    *out = {};
    strncpy(out->error, message, sizeof(out->error) - 1);
}

}  // namespace

bool batteryBegin() {
    gReady = false;
    gAddress = 0;
    gCached = {};

    gUpsWire.begin(UPS_BATTERY_SDA, UPS_BATTERY_SCL, 100000);
    delay(10);

    for (uint8_t address : kProbeAddresses) {
        if (!probeAddress(address)) {
            continue;
        }
        gAddress = address;
        if (!configureIna219()) {
            gAddress = 0;
            continue;
        }
        delay(20);
        gReady = true;
        Serial.printf("Battery: INA219 detected at 0x%02X (SDA=%d SCL=%d)\n",
                      gAddress, UPS_BATTERY_SDA, UPS_BATTERY_SCL);
        return batteryRead(&gCached);
    }

    Serial.printf(
        "Battery: INA219 not found on SDA=%d SCL=%d (checked 0x40-0x45)\n",
        UPS_BATTERY_SDA, UPS_BATTERY_SCL);
    fillUnavailable(&gCached, "ina219 not found");
    return false;
}

bool batteryRead(BatteryTelemetry *out) {
    if (out == nullptr) {
        return false;
    }

    if (!gReady) {
        fillUnavailable(out, gCached.error[0] ? gCached.error : "not initialized");
        return false;
    }

    if (!writeRegister16(kRegCalibration, kCalibrationValue)) {
        fillUnavailable(out, "calibration write failed");
        return false;
    }

    BatteryTelemetry sample;
    sample.available = true;
    sample.i2cAddress = gAddress;

    const int16_t shuntRaw = readRegister16(kRegShuntVoltage);
    const int16_t busRaw = readRegister16(kRegBusVoltage);
    const int16_t currentRaw = readRegister16(kRegCurrent);
    const int16_t powerRaw = readRegister16(kRegPower);

    sample.shuntVoltageMv = static_cast<float>(shuntRaw) * 0.01f;
    sample.busVoltageV = static_cast<float>(busRaw >> 3) * 0.004f;
    sample.currentMa = static_cast<float>(currentRaw) * kCurrentLsbMa;
    sample.powerMw = static_cast<float>(powerRaw) * kPowerLsbMw;
    sample.socPct = estimateSocPct(sample.busVoltageV);

    if (sample.busVoltageV <= 0.05f) {
        fillUnavailable(out, "invalid bus voltage");
        return false;
    }

    *out = sample;
    gCached = sample;
    return true;
}

void batteryPrint(const BatteryTelemetry &sample) {
    if (!sample.available) {
        Serial.printf("Battery: unavailable (%s)\n",
                      sample.error[0] ? sample.error : "unknown");
        return;
    }
    Serial.printf(
        "Battery: %.2f V  %.1f mA  %.0f mW  SoC~%d%%  (INA219 0x%02X)\n",
        sample.busVoltageV, sample.currentMa, sample.powerMw, sample.socPct,
        sample.i2cAddress);
}

BatteryTelemetry batteryGetCached() {
    return gCached;
}
