#include "nfc_wake.h"
#include "nfc_config.h"
#include "power/power.h"

#include <Wire.h>
#include <esp_sleep.h>

namespace {

constexpr int kNfcSda = NFC_SDA;
constexpr int kNfcScl = NFC_SCL;
constexpr int kNfcInt = NFC_INT;

constexpr uint8_t ST25DV_I2C_USER = 0x53;
constexpr uint8_t ST25DV_I2C_SYSTEM = 0x57;

constexpr uint16_t ST25DV_REG_GPO = 0x0000;
constexpr uint16_t ST25DV_REG_IT_TIME = 0x0001;
constexpr uint16_t ST25DV_REG_RF_MNGT = 0x0003;
constexpr uint16_t ST25DV_DYN_RF_MNGT = 0x2003;
constexpr uint16_t ST25DV_REG_ICREF = 0x0017;
constexpr uint16_t ST25DV_REG_I2C_PWD = 0x0900;
constexpr uint16_t ST25DV_DYN_GPO_CTRL = 0x2000;
constexpr uint16_t ST25DV_DYN_IT_STS = 0x2005;

constexpr uint8_t ST25DV_GPO_FIELDCHANGE = 0x08;
constexpr uint8_t ST25DV_GPO_ENABLE = 0x80;
constexpr uint8_t ST25DV_RF_NORMAL = 0x00;

class St25Dv16k {
 public:
    bool begin() {
        if (!i2cProbe(ST25DV_I2C_USER)) {
            return false;
        }
        uint8_t icref = 0;
        return readSystemReg(ST25DV_REG_ICREF, &icref);
    }

    bool presentI2cPassword() {
        uint8_t msg[17] = {};
        msg[8] = 0x09;
        return i2cWriteReg(ST25DV_I2C_SYSTEM, ST25DV_REG_I2C_PWD, msg, sizeof(msg));
    }

    bool readDynamicReg(uint16_t reg, uint8_t *value) {
        return i2cReadReg(ST25DV_I2C_USER, reg, value, 1);
    }

    bool readSystemReg(uint16_t reg, uint8_t *value) {
        return i2cReadReg(ST25DV_I2C_SYSTEM, reg, value, 1);
    }

    bool writeSystemReg(uint16_t reg, uint8_t value) {
        if (!i2cWriteByte(ST25DV_I2C_SYSTEM, reg, value)) {
            return false;
        }
        delay(5);
        return true;
    }

    bool writeDynamicReg(uint16_t reg, uint8_t value) {
        return i2cWriteByte(ST25DV_I2C_USER, reg, value);
    }

    bool setRfNormalDynamic() {
        return writeDynamicReg(ST25DV_DYN_RF_MNGT, ST25DV_RF_NORMAL);
    }

    bool setRfNormalStatic() {
        return writeSystemReg(ST25DV_REG_RF_MNGT, ST25DV_RF_NORMAL);
    }

    bool setRfNormal() {
        return setRfNormalDynamic() && setRfNormalStatic();
    }

    bool readAndClearItStatus(uint8_t *status) {
        return readDynamicReg(ST25DV_DYN_IT_STS, status);
    }

    bool configureFieldChangeGpo() {
        if (!presentI2cPassword()) {
            Serial.println("NFC: I2C password failed");
            return false;
        }
        if (!writeSystemReg(ST25DV_REG_IT_TIME, 0x00)) {
            return false;
        }
        const uint8_t gpoConfig = ST25DV_GPO_ENABLE | ST25DV_GPO_FIELDCHANGE;
        if (!writeSystemReg(ST25DV_REG_GPO, gpoConfig)) {
            return false;
        }
        if (!setRfNormal()) {
            return false;
        }
        uint8_t gpoDyn = 0;
        if (!readDynamicReg(ST25DV_DYN_GPO_CTRL, &gpoDyn)) {
            return false;
        }
        gpoDyn |= ST25DV_GPO_ENABLE;
        return i2cWriteByte(ST25DV_I2C_USER, ST25DV_DYN_GPO_CTRL, gpoDyn);
    }

 private:
    static bool i2cProbe(uint8_t addr) {
        Wire.beginTransmission(addr);
        return Wire.endTransmission(true) == 0;
    }

    static bool i2cWriteReg(uint8_t devAddr, uint16_t reg, const uint8_t *data, size_t len) {
        Wire.beginTransmission(devAddr);
        Wire.write(static_cast<uint8_t>(reg >> 8));
        Wire.write(static_cast<uint8_t>(reg & 0xFF));
        for (size_t i = 0; i < len; ++i) {
            Wire.write(data[i]);
        }
        return Wire.endTransmission(true) == 0;
    }

    static bool i2cWriteByte(uint8_t devAddr, uint16_t reg, uint8_t value) {
        return i2cWriteReg(devAddr, reg, &value, 1);
    }

    static bool i2cReadReg(uint8_t devAddr, uint16_t reg, uint8_t *data, size_t len) {
        Wire.beginTransmission(devAddr);
        Wire.write(static_cast<uint8_t>(reg >> 8));
        Wire.write(static_cast<uint8_t>(reg & 0xFF));
        if (Wire.endTransmission(false) != 0) {
            return false;
        }
        if (Wire.requestFrom(devAddr, static_cast<uint8_t>(len)) != len) {
            return false;
        }
        for (size_t i = 0; i < len; ++i) {
            data[i] = Wire.read();
        }
        return true;
    }
};

St25Dv16k gNfc;
volatile bool gNfcTapFlag = false;
volatile bool gTapRequested = false;

void IRAM_ATTR nfcGpoIsr() {
    gNfcTapFlag = true;
}

void beginNfcI2c() {
    Wire.setPins(kNfcSda, kNfcScl);
    Wire.begin();
    Wire.setClock(400000);
}

bool waitIntPinInactive(uint32_t timeoutMs) {
    const uint32_t start = millis();
    while (digitalRead(kNfcInt) == LOW) {
        if (millis() - start > timeoutMs) {
            return false;
        }
        delay(1);
    }
    return true;
}

}  // namespace

bool nfcBegin() {
    beginNfcI2c();
    if (!gNfc.begin()) {
        Serial.println("NFC: ST25DV16K not detected on I2C");
        return false;
    }
    Serial.printf("NFC: ST25DV16K detected (SDA=%d SCL=%d INT=%d)\n",
                  kNfcSda, kNfcScl, kNfcInt);
    return true;
}

bool nfcConfigureWake() {
    if (!gNfc.configureFieldChangeGpo()) {
        Serial.println("NFC: FIELD_CHANGE GPO config failed");
        return false;
    }
    Serial.println("NFC: FIELD_CHANGE GPO configured for wake-on-tap");
    return true;
}

void nfcOnWake() {
    beginNfcI2c();
    if (gNfc.begin()) {
        gNfc.setRfNormalDynamic();
        uint8_t itStatus = 0;
        gNfc.readAndClearItStatus(&itStatus);
    }
}

void nfcAttachTapMonitor() {
    pinMode(kNfcInt, INPUT);
    attachInterrupt(digitalPinToInterrupt(kNfcInt), nfcGpoIsr, FALLING);
}

void nfcPollTapMonitor() {
    if (!gNfcTapFlag) {
        return;
    }
    gNfcTapFlag = false;
    uint8_t itStatus = 0;
    gNfc.readAndClearItStatus(&itStatus);
    Serial.printf("NFC tap detected (INT=GPIO%d, IT_STS=0x%02X)\n", kNfcInt, itStatus);
    gTapRequested = true;
    activityBump();
}

bool nfcConsumeTapRequest() {
    if (!gTapRequested) {
        return false;
    }
    gTapRequested = false;
    return true;
}

void nfcSignalTapRequest() {
    gTapRequested = true;
    activityBump();
}

bool nfcEnterDeepSleep() {
    detachInterrupt(digitalPinToInterrupt(kNfcInt));

    beginNfcI2c();
    uint8_t itStatus = 0;
    gNfc.readAndClearItStatus(&itStatus);

    pinMode(kNfcInt, INPUT);
    if (!waitIntPinInactive(500)) {
        Serial.println("NFC: INT still LOW — sleeping anyway");
    }

    if (!gNfc.setRfNormalDynamic()) {
        Serial.println("NFC: failed to keep RF active before sleep");
    }

    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
    esp_sleep_enable_ext0_wakeup(static_cast<gpio_num_t>(kNfcInt), 0);

    Serial.printf("Entering deep sleep (wake on NFC tap, idle=%lus)\n",
                  static_cast<unsigned long>(IDLE_SLEEP_MS / 1000));
    Serial.flush();
    esp_deep_sleep_start();
    return false;
}
