#include "display_panel.h"

#include "DEV_Config.h"
#include "EPD_13in3e.h"

namespace {

constexpr size_t kRowBytes = EPD_13IN3E_WIDTH / 2;
constexpr size_t kHalfRowBytes = kRowBytes / 2;
constexpr size_t kExpectedImageBytes = kRowBytes * EPD_13IN3E_HEIGHT;
bool gPanelInitialized = false;

void sendHalf(const uint8_t *image, int chipSelectPin, size_t rowOffset) {
    DEV_Digital_Write(chipSelectPin, 0);
    DEV_Digital_Write(EPD_DC_PIN, 0);
    delay(300);
    EPD_13IN3E_SendCommand(0x10);

    for (size_t row = 0; row < EPD_13IN3E_HEIGHT; ++row) {
        const uint8_t *rowData = image + row * kRowBytes + rowOffset;
        EPD_13IN3E_SendData2(rowData, kHalfRowBytes);
        delay(1);
    }

    EPD_13IN3E_CS_ALL(1);
}

}  // namespace

bool displayShowPackedImage(const uint8_t *data, size_t len) {
    if (len != kExpectedImageBytes) {
        Serial.printf("Display buffer wrong size: %u (expected %u)\n",
                      static_cast<unsigned>(len),
                      static_cast<unsigned>(kExpectedImageBytes));
        return false;
    }

    DEV_Module_Init();
    EPD_13IN3E_Init();
    gPanelInitialized = true;

    Serial.println("Sending image to CS0...");
    sendHalf(data, EPD_CS_M_PIN, 0);

    Serial.println("Sending image to CS1...");
    sendHalf(data, EPD_CS_S_PIN, kHalfRowBytes);

    Serial.println("Refreshing panel (~30 s)...");
    if (!EPD_13IN3E_TurnOnDisplay()) {
        Serial.println("Display update failed.");
        return false;
    }
    Serial.println("Display update complete.");
    return true;
}

void displaySleepBeforePowerOff() {
    if (!gPanelInitialized) {
        return;
    }

    Serial.println("Putting panel controller into deep sleep...");
    EPD_13IN3E_Sleep();
    delay(100);
    DEV_Module_Exit();
    gPanelInitialized = false;
    Serial.println("Panel controller asleep.");
}
