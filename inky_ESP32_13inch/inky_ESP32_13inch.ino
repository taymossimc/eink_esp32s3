#include "EPD_13in3e.h"

#include "DEV_Config.h"

#include "carousel/carousel.h"
#include "battery/ups_battery.h"
#include "config/device_config.h"

#include "display/display_panel.h"
#include "display/display_identity.h"

#include "modem/modem.h"
#include "log/phone_home_log.h"

#include "nfc/nfc_wake.h"

#include "nfc/nfc_config.h"

#include "config/firmware_version.h"
#include "network/network_manager.h"
#include "power/power.h"
#include "portal/config_portal.h"
#include "sms/sms_store.h"
#include "storage/usb_campaign_storage.h"
#include "tmads/tmads_client.h"
#include "tmads/tmads_identity.h"
#include "tmads/tmads_sync.h"

#include <SPI.h>

#include <esp_sleep.h>



#include "tmads/tmads_timing.h"

#include "cache/frame_cache.h"

namespace {

uint32_t gLastTmadsPollMs = 0;

uint32_t tmadsPollIntervalMs() {
    return tmads::identityAwakePollSec() * 1000UL;
}


const char *wakeCauseString() {

    switch (esp_sleep_get_wakeup_cause()) {

        case ESP_SLEEP_WAKEUP_EXT0:

            return "NFC tap (EXT0)";

        case ESP_SLEEP_WAKEUP_TIMER:

            return "timer";

        case ESP_SLEEP_WAKEUP_UNDEFINED:

            return "Power-on / reset";

        default:

            return "Other";

    }

}



void enterIdleSleep() {

    Serial.println("\nIdle timeout — collecting final status...");

    const ModemDiagnostics modemStatus = modemGetDiagnostics();
    if (modemStatus.ready) {
        float latitude = 0, longitude = 0, altitude = 0;
        int satellites = 0;
        modemGetGnssFix(&latitude, &longitude, &altitude, &satellites, nullptr);
    }

    if (networkWifiConnected() || modemIsDataConnected()) {
        const uint32_t preSleepStart = millis();
        const tmads::CheckInResult finalStatus =
            tmads::clientCheckIn("pre-sleep");
        tmads::timings().preSleepReportMs = millis() - preSleepStart;
        if (finalStatus.ok) {
            Serial.println("TMAds: pre-sleep status sent");
        } else {
            Serial.printf("TMAds: pre-sleep status failed: %s\n",
                          finalStatus.error.c_str());
        }
    } else {
        Serial.println("TMAds: pre-sleep status skipped (offline)");
    }

    const BatteryTelemetry battery = batteryGetCached();
    const bool lowBattery =
        battery.available && battery.socPct >= 0 && battery.socPct < 15;
    tmads::identitySetLowBatteryOverride(lowBattery);
    if (lowBattery) {
        Serial.printf("TMAds: low-battery wake budget enabled (%d%%)\n",
                      battery.socPct);
    }

    Serial.println("Shutting down 5V rail and sleeping...");

    configPortalStop();

    displaySleepBeforePowerOff();

    displayPinsSafe();

    modemReleaseData();

    powerRailSet(false);

    nfcEnterDeepSleep();

}



bool runModemBringUp() {

    modemStartUartEarly();

    modemListenDuringPowerUp();

    if (!modemBegin()) {

        Serial.println("Modem init failed");
        return false;

    }

    modemPrintSimNumber();

    modemPollQueuedSms();

    if (!modemEnableGnss()) {

        Serial.println("GNSS init failed");

    }
    float latitude = 0, longitude = 0, altitude = 0;
    int satellites = 0;
    modemGetGnssFix(&latitude, &longitude, &altitude, &satellites, nullptr);

    return true;

}



bool handleCarouselUpdate(const char *reason) {

    Serial.printf("\nCarousel update (%s)\n", reason);

    activityBump();

    if (!carouselFetchAndDisplay()) {

        Serial.println("Carousel update failed — panel unchanged");

        return false;

    }

    Serial.printf("Now showing: %s\n", carouselCurrentImageName());

    return true;

}

bool handleCarouselPrefetch() {

    Serial.println("\nPreparing next cached image...");

    activityBump();

    if (!carouselFetchAndCache()) {

        Serial.println("Next-image prefetch failed");

        return false;

    }

    Serial.println("Next image cached and ready.");

    activityBump();

    return true;

}



String readSerialLine(uint32_t timeoutMs = 8000) {
    String line;
    const uint32_t end = millis() + timeoutMs;
    while (millis() < end) {
        while (Serial.available()) {
            const char c = static_cast<char>(Serial.read());
            if (c == '\r' || c == '\n') {
                if (line.length() > 0) return line;
            } else {
                line += c;
            }
        }
        delay(10);
    }
    return line;
}

void handleAwakeContentCycle(const char *reason) {

    Serial.printf("\nContent cycle (%s)\n", reason);

    activityBump();

    if (!carouselDisplayCached()) {

        handleCarouselUpdate("cache miss");

    }

    handleCarouselPrefetch();

    activityBump();

}



}  // namespace



void setup() {

    Serial.begin(115200);

    delay(500);



    const bool wokeFromTap =

        (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT0);



    Serial.println("\nESP32-S3 Inky 13.3 + SIM7600 + NFC TapWake");

    Serial.printf("Wake: %s\n", wakeCauseString());

    Serial.printf(

        "Display: %dx%d  NFC SDA=%d SCL=%d INT=%d  5V=GPIO%d\n",

        EPD_13IN3E_WIDTH, EPD_13IN3E_HEIGHT, NFC_SDA, NFC_SCL, NFC_INT,

        POWER_ENABLE_GPIO);



    powerRailInit();

    // A USB reset can occur while the rail is already on. Hold it low long
    // enough for the modem to fully discharge before starting a clean boot.
    delay(3000);

    powerRailSet(true);



    if (wokeFromTap) {

        Serial.println("NFC WAKE — 5V rail enabled");

        nfcOnWake();

        nfcSignalTapRequest();

    }



    if (!nfcBegin()) {

        Serial.println("WARNING: NFC init failed");

    } else if (!nfcConfigureWake()) {

        Serial.println("WARNING: NFC GPO config failed");

    } else if (!nfcWriteUrl(NFC_SHELF_URL)) {

        Serial.println("WARNING: NFC shelf URL write failed");

    }

    displayIdentityProbe();



    nfcAttachTapMonitor();

    activityBump();

    device_config::load();
    BatteryTelemetry battery;
    batteryBegin();
    batteryRead(&battery);
    batteryPrint(battery);

    if (!carouselCacheBegin()) {

        Serial.println("WARNING: persistent frame cache unavailable");

    }

    if (!smsStoreBegin()) {
        Serial.println("WARNING: persistent SMS store unavailable");
    }
    if (!phone_home_log::begin()) {
        Serial.println("WARNING: phone-home log unavailable");
    }
    if (!usbCampaignStorageBegin()) {
        Serial.println("WARNING: USB campaign storage host unavailable");
    }
    if (!configPortalBegin()) {
        Serial.println("WARNING: configuration portal unavailable");
    }

    // Apply the fleet-managed awake timeout before any network work.
    tmads::identityBegin();
    activitySetTimeoutMs(tmads::identityAwakeTimeoutSec() * 1000UL);

    // Boot fast path: show a locally prepared "next" frame if one is queued
    // (portal uploads). The e-ink panel retains the last TMAds frame across
    // deep sleep, so re-displaying "current" is unnecessary.
    bool displayedCached = carouselDisplayCached();
    if (!displayedCached && frameCacheSlotAvailable(FrameCacheSlot::Current)) {
        // The panel already shows the persisted current frame.
        displayedCached = true;
    }

    tmads::timings().bootToSyncMs = millis();
    networkManagerBegin();
    Serial.println("WiFi: trying saved networks (most-recent-first)...");
    const uint32_t connectStart = millis();
    const bool wifiReady = networkConnectSaved(25000);
    if (wifiReady) {
        tmads::timings().connectMs = millis() - connectStart;
        Serial.printf("WiFi: online at %s (%s)\n",
                      networkWifiSsid().c_str(),
                      networkWifiIp().c_str());
    } else {
        Serial.println("WiFi: unavailable — cellular fallback will be used");
    }

    if (wifiReady) {
        const tmads::SyncOutcome syncOutcome =
            tmads::syncOnWake(wakeCauseString());
        gLastTmadsPollMs = millis();
        if (syncOutcome == tmads::SyncOutcome::FirmwareUpdated) {
            return;
        }
    }

    // The modem powers up on interactive wakes (GNSS, SMS) and — unlike the
    // previous firmware — also on timer wakes when WiFi is unavailable, so
    // scheduled syncs still reach the server in the field.
    const bool timerWake =
        esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER;
    const bool needModem = !timerWake || !wifiReady;
    const uint32_t modemStart = millis();
    const bool modemReady = needModem && runModemBringUp();
    if (!wifiReady && modemReady && modemEnsureDataConnected()) {
        tmads::timings().connectMs = millis() - modemStart;
        const tmads::SyncOutcome syncOutcome =
            tmads::syncOnWake(wakeCauseString());
        gLastTmadsPollMs = millis();
        if (syncOutcome == tmads::SyncOutcome::FirmwareUpdated) {
            return;
        }
    }

    if (!displayedCached) {
        Serial.println(
            "Campaign cache empty; waiting for TMAds campaign sync");
    }

    // The wake request has now been serviced. A later tap remains queued by ISR.
    nfcConsumeTapRequest();
    activityBump();



    Serial.printf("Awake. Deep sleep after %lu s without NFC tap.\n",

                  static_cast<unsigned long>(activityTimeoutMs() / 1000));

    Serial.printf("Firmware %s (%s)\n", FIRMWARE_VERSION, FIRMWARE_BUILD);
    Serial.println(
        "Commands: e=TMAds sync  l=GPS  m=check SMS  n=SIM number  "
        "s=sleep now  t=TMAds status  T=set enroll token");

}



void loop() {

    configPortalProcessActions();

    nfcPollTapMonitor();



    if (nfcConsumeTapRequest()) {

        if (networkWifiConnected() || modemIsDataConnected() ||
            networkConnectSaved(15000)) {
            const tmads::SyncOutcome outcome = tmads::syncOnWake("nfc tap");
            gLastTmadsPollMs = millis();
            if (outcome == tmads::SyncOutcome::FirmwareUpdated) return;
        }
    }

    if (millis() - gLastTmadsPollMs >= tmadsPollIntervalMs() &&
        (networkWifiConnected() || modemIsDataConnected())) {
        const tmads::SyncOutcome outcome = tmads::syncOnWake("awake-poll");
        gLastTmadsPollMs = millis();
        if (outcome == tmads::SyncOutcome::FirmwareUpdated) return;
    }



    if (activityTimedOut()) {

        enterIdleSleep();

    }



    if (Serial.available()) {

        const char cmd = static_cast<char>(Serial.read());

        activityBump();

        if (cmd == 'c') {
            Serial.println(
                "Legacy carousel fetch disabled; use TMAds campaign sync");

        } else if (cmd == 'e') {
            if (networkConnectSaved(30000)) {
                const tmads::SyncOutcome outcome =
                    tmads::syncOnWake("serial");
                Serial.printf("TMAds sync outcome: %d\n",
                              static_cast<int>(outcome));
            } else {
                Serial.println("TMAds sync requires Wi-Fi");
            }

        } else if (cmd == 't') {
            tmads::identityBegin();
            Serial.printf("TMAds device: %s enrolled=%s boot=%s wake=%us\n",
                          tmads::identityDeviceId(),
                          tmads::identityIsEnrolled() ? "yes" : "no",
                          tmads::identityBootId().c_str(),
                          tmads::identityWakeIntervalSec());
            Serial.printf("TMAds public key: %s\n",
                          tmads::identityPublicKeyWire().c_str());

        } else if (cmd == 'T') {
            Serial.println("TMAds: paste enrollment token:");
            const String token = readSerialLine();
            tmads::identitySetEnrollmentToken(token.c_str());
            Serial.println("TMAds: enrollment token saved");

        } else if (cmd == 'l') {

            modemPrintGnss();

        } else if (cmd == 'n') {

            modemPrintSimNumber();

        } else if (cmd == 'm') {

            modemPollQueuedSms();

        } else if (cmd == 's') {

            enterIdleSleep();

        }

    }



    modemPumpSerial();

    delay(50);

}
