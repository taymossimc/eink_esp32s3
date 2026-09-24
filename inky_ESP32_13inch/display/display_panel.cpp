#include "display_panel.h"

#include "DEV_Config.h"
#include "EPD_13in3e.h"
#include "power/power.h"

#include <SPI.h>
#include <string.h>

namespace {

constexpr size_t kRowBytes = EPD_13IN3E_WIDTH / 2;
constexpr size_t kHalfRowBytes = kRowBytes / 2;
constexpr size_t kExpectedImageBytes = kRowBytes * EPD_13IN3E_HEIGHT;
bool gPanelInitialized = false;
char gLastError[96] = {};
uint32_t gLastTransferMs = 0;
uint32_t gLastBusyRefreshMs = 0;
uint32_t gLastSuccessfulRefreshMs = 0;

void setDisplayError(const char *message) {
    strncpy(gLastError, message ? message : "", sizeof(gLastError) - 1);
    gLastError[sizeof(gLastError) - 1] = '\0';
}

void sendHalf(const uint8_t *image, int chipSelectPin, size_t rowOffset) {
    SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
    DEV_Digital_Write(chipSelectPin, 0);
    DEV_Digital_Write(EPD_DC_PIN, 0);
    delay(300);
    SPI.transfer(0x10);
    DEV_Digital_Write(EPD_DC_PIN, 1);

    for (size_t row = 0; row < EPD_13IN3E_HEIGHT; ++row) {
        const uint8_t *rowData = image + row * kRowBytes + rowOffset;
        SPI.writeBytes(const_cast<uint8_t *>(rowData), kHalfRowBytes);
        delay(1);
    }

    DEV_Digital_Write(chipSelectPin, 1);
    DEV_Digital_Write(EPD_DC_PIN, 0);
    SPI.endTransaction();
}

bool transferAndRefresh(const uint8_t *data) {
    const uint32_t transferStart = millis();
    Serial.println("Sending image to CS0...");
    sendHalf(data, EPD_CS_M_PIN, 0);

    Serial.println("Sending image to CS1...");
    sendHalf(data, EPD_CS_S_PIN, kHalfRowBytes);
    gLastTransferMs += millis() - transferStart;

    Serial.println("Refreshing panel (~30 s)...");
    const uint32_t refreshStart = millis();
    const bool refreshed = EPD_13IN3E_TurnOnDisplay();
    gLastBusyRefreshMs += millis() - refreshStart;
    return refreshed;
}

}  // namespace

bool displayShowPackedImage(const uint8_t *data, size_t len) {
    setDisplayError("");
    if (len != kExpectedImageBytes) {
        Serial.printf("Display buffer wrong size: %u (expected %u)\n",
                      static_cast<unsigned>(len),
                      static_cast<unsigned>(kExpectedImageBytes));
        setDisplayError("packed framebuffer has wrong size");
        return false;
    }
    constexpr uint32_t kMinimumRefreshSpacingMs = 120000;
    if (gLastSuccessfulRefreshMs != 0) {
        const uint32_t elapsed = millis() - gLastSuccessfulRefreshMs;
        if (elapsed < kMinimumRefreshSpacingMs) {
            const uint32_t waitMs = kMinimumRefreshSpacingMs - elapsed;
            Serial.printf("Display cooldown: waiting %u ms before refresh\n",
                          waitMs);
            delay(waitMs);
        }
    }
    if (gPanelInitialized) {
        Serial.println("Display: cleanly resetting initialized controller");
        EPD_13IN3E_Sleep();
        delay(100);
        DEV_Module_Exit();
        gPanelInitialized = false;
        delay(500);
    }

    DEV_Module_Init();
    EPD_13IN3E_Init();
    gPanelInitialized = true;
    gLastTransferMs = 0;
    gLastBusyRefreshMs = 0;

    if (!transferAndRefresh(data)) {
        // An occasional controller start leaves BUSY idle after DRF. A logic
        // reset alone does not reliably clear that latched state on PIM774,
        // so fully discharge the shared 5 V rail before retransmitting once.
        Serial.println(
            "Display refresh did not start/complete; power-cycling controller once");
        DEV_Module_Exit();
        gPanelInitialized = false;
        displayPinsSafe();
        powerRailSet(false);
        delay(3000);
        powerRailSet(true);
        delay(3000);
        DEV_Module_Init();
        EPD_13IN3E_Init();
        gPanelInitialized = true;
        if (!transferAndRefresh(data)) {
            Serial.println("Display update failed after controller retry.");
            setDisplayError(
                "panel refresh failed after controller reset/retry");
            return false;
        }
    }
    Serial.println("Display update complete.");
    gLastSuccessfulRefreshMs = millis();
    return true;
}

const char *displayLastError() { return gLastError; }

bool displayPanelInitialized() { return gPanelInitialized; }

uint32_t displayLastTransferMs() { return gLastTransferMs; }

uint32_t displayLastBusyRefreshMs() { return gLastBusyRefreshMs; }

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
