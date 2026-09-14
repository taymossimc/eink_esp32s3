#include "power.h"
#include "nfc/nfc_config.h"
#include "DEV_Config.h"
#include <SPI.h>

namespace {

bool gRailOn = false;
uint32_t gLastActivityMs = 0;

}  // namespace

void powerRailInit() {
    pinMode(POWER_ENABLE_GPIO, OUTPUT);
    digitalWrite(POWER_ENABLE_GPIO, LOW);
    gRailOn = false;
    gLastActivityMs = millis();
}

void powerRailSet(bool on) {
    digitalWrite(POWER_ENABLE_GPIO, on ? HIGH : LOW);
    gRailOn = on;
    Serial.printf("5V rail (GPIO%d): %s\n", POWER_ENABLE_GPIO, on ? "ON" : "OFF");
}

bool powerRailIsOn() {
    return gRailOn;
}

void activityBump() {
    gLastActivityMs = millis();
}

uint32_t activityIdleMs() {
    return millis() - gLastActivityMs;
}

bool activityTimedOut() {
    return activityIdleMs() >= IDLE_SLEEP_MS;
}

void displayPinsSafe() {
    SPI.end();
    pinMode(EPD_RST_PIN, OUTPUT);
    pinMode(EPD_DC_PIN, OUTPUT);
    pinMode(EPD_CS_M_PIN, OUTPUT);
    pinMode(EPD_CS_S_PIN, OUTPUT);
    pinMode(EPD_MOSI_PIN, OUTPUT);
    pinMode(EPD_SCK_PIN, OUTPUT);
    digitalWrite(EPD_RST_PIN, LOW);
    digitalWrite(EPD_DC_PIN, LOW);
    digitalWrite(EPD_CS_M_PIN, LOW);
    digitalWrite(EPD_CS_S_PIN, LOW);
    digitalWrite(EPD_MOSI_PIN, LOW);
    digitalWrite(EPD_SCK_PIN, LOW);
}
