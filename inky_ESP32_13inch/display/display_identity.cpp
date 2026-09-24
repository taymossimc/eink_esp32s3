#include "display_identity.h"

#include <Wire.h>

namespace {

constexpr uint8_t kEepromAddress = 0x50;
constexpr size_t kIdentityLength = 29;
DisplayIdentity gIdentity;

}  // namespace

bool displayIdentityProbe() {
    gIdentity = {};

    // The Inky ID EEPROM uses a 16-bit memory address. Pimoroni's SMBus call
    // writes command 0x00 plus data byte 0x00 before reading 29 bytes.
    Wire.beginTransmission(kEepromAddress);
    Wire.write(static_cast<uint8_t>(0x00));
    Wire.write(static_cast<uint8_t>(0x00));
    if (Wire.endTransmission(true) != 0) {
        Serial.println("Display EEPROM: 0x50 not detected");
        return false;
    }
    const size_t received = Wire.requestFrom(
        static_cast<int>(kEepromAddress), static_cast<int>(kIdentityLength));
    if (received != kIdentityLength) {
        Serial.printf("Display EEPROM: short read (%u/29)\n",
                      static_cast<unsigned>(received));
        while (Wire.available()) Wire.read();
        return false;
    }

    uint8_t data[kIdentityLength] = {};
    for (size_t i = 0; i < kIdentityLength; ++i) {
        data[i] = static_cast<uint8_t>(Wire.read());
    }
    gIdentity.present = true;
    gIdentity.width =
        static_cast<uint16_t>(data[0]) |
        (static_cast<uint16_t>(data[1]) << 8);
    gIdentity.height =
        static_cast<uint16_t>(data[2]) |
        (static_cast<uint16_t>(data[3]) << 8);
    gIdentity.color = data[4];
    gIdentity.pcbVariant = data[5];
    gIdentity.displayVariant = data[6];

    Serial.printf(
        "Display EEPROM: %ux%u color=%u pcb=%u variant=%u%s\n",
        gIdentity.width, gIdentity.height, gIdentity.color,
        gIdentity.pcbVariant, gIdentity.displayVariant,
        gIdentity.displayVariant == 21 ? " (PIM774 EL133UF1)" : "");
    return true;
}

DisplayIdentity displayIdentityGet() { return gIdentity; }
