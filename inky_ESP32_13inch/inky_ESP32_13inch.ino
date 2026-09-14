#include "EPD_13in3e.h"

#include "DEV_Config.h"

#include "carousel/carousel.h"
#include "config/device_config.h"

#include "display/display_panel.h"

#include "modem/modem.h"
#include "log/phone_home_log.h"

#include "nfc/nfc_wake.h"

#include "nfc/nfc_config.h"

#include "power/power.h"
#include "portal/config_portal.h"
#include "sms/sms_store.h"

#include <SPI.h>

#include <esp_sleep.h>



namespace {



const char *wakeCauseString() {

    switch (esp_sleep_get_wakeup_cause()) {

        case ESP_SLEEP_WAKEUP_EXT0:

            return "NFC tap (EXT0)";

        case ESP_SLEEP_WAKEUP_UNDEFINED:

            return "Power-on / reset";

        default:

            return "Other";

    }

}



void enterIdleSleep() {

    Serial.println("\nIdle timeout — shutting down 5V rail and sleeping...");

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

    }



    nfcAttachTapMonitor();

    activityBump();

    device_config::load();

    if (!carouselCacheBegin()) {

        Serial.println("WARNING: persistent frame cache unavailable");

    }

    if (!smsStoreBegin()) {
        Serial.println("WARNING: persistent SMS store unavailable");
    }
    if (!phone_home_log::begin()) {
        Serial.println("WARNING: phone-home log unavailable");
    }
    if (!configPortalBegin()) {
        Serial.println("WARNING: configuration portal unavailable");
    }

    // The cached framebuffer is intentionally displayed before modem startup.
    const bool displayedCached = carouselDisplayCached();

    const bool modemReady = runModemBringUp();

    if (modemReady) {

        if (!displayedCached) {

            handleCarouselUpdate(wokeFromTap ? "wake cache miss"
                                             : "power-on cache miss");

        }

        handleCarouselPrefetch();

    }

    // The wake request has now been serviced. A later tap remains queued by ISR.
    nfcConsumeTapRequest();
    activityBump();



    Serial.printf("Awake. Deep sleep after %lu s without NFC tap.\n",

                  static_cast<unsigned long>(IDLE_SLEEP_MS / 1000));

    Serial.println(
        "Commands: c=carousel  l=GPS  m=check SMS  n=SIM number  s=sleep now");

}



void loop() {

    configPortalProcessActions();

    nfcPollTapMonitor();



    if (nfcConsumeTapRequest()) {

        handleAwakeContentCycle("nfc tap");

    }



    if (activityTimedOut()) {

        enterIdleSleep();

    }



    if (Serial.available()) {

        const char cmd = static_cast<char>(Serial.read());

        activityBump();

        if (cmd == 'c') {

            handleAwakeContentCycle("serial");

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
