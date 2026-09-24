#include "tmads_sync.h"

#include "battery/ups_battery.h"
#include "cache/frame_cache.h"
#include "config/device_config.h"
#include "tmads_client.h"
#include "tmads_identity.h"
#include "tmads_timing.h"

#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

namespace tmads {
namespace {

struct CampaignTaskContext {
    CampaignSyncResult result;
    TaskHandle_t caller = nullptr;
};

void campaignSyncTask(void *argument) {
    auto *context = static_cast<CampaignTaskContext *>(argument);
    context->result = clientSyncCampaigns();
    xTaskNotifyGive(context->caller);
    vTaskDelete(nullptr);
}

CampaignSyncResult runCampaignSync() {
    CampaignTaskContext context;
    context.caller = xTaskGetCurrentTaskHandle();
    TaskHandle_t task = nullptr;
    if (xTaskCreatePinnedToCore(campaignSyncTask, "tmads-campaign", 32768,
                                &context, 1, &task, 0) != pdPASS) {
        context.result.error = "campaign worker start failed";
        return context.result;
    }
    // A full-color refresh can include the 120 s panel cooldown and one
    // controller retry. Leave enough time for that recovery path plus
    // transport work before declaring the worker hung.
    if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(720000)) == 0) {
        vTaskDelete(task);
        context.result.error = "campaign worker timed out";
    }
    Preferences prefs;
    if (prefs.begin("tmads_campaign", false)) {
        if (context.result.ok) {
            if (context.result.assetErrors.length()) {
                prefs.putString("last_error", context.result.assetErrors);
            } else {
                prefs.remove("last_error");
            }
        } else {
            prefs.putString("last_error", context.result.error);
        }
        prefs.putUInt("last_attempt", millis() / 1000U);
        prefs.end();
    }
    return context.result;
}

// Applies a fleet-managed orientation. Incompatible packed frames are
// invalidated so the next campaign display re-renders and refreshes once.
void applyServerOrientation(int orientationDegrees) {
    if (orientationDegrees < 0) return;
    device_config::DisplayOrientation parsed;
    char text[8] = {};
    snprintf(text, sizeof(text), "%d", orientationDegrees);
    if (!device_config::parseDisplayOrientation(text, parsed)) return;
    const auto current = device_config::getSnapshot();
    if (current.displayOrientation == parsed) return;
    Serial.printf("TMAds: fleet orientation change %u -> %d\n",
                  static_cast<unsigned>(current.displayOrientation),
                  orientationDegrees);
    auto updated = current;
    updated.displayOrientation = parsed;
    if (!device_config::save(updated)) {
        Serial.println("TMAds: orientation save failed");
        return;
    }
    frameCacheRemoveSlot(FrameCacheSlot::Current);
    frameCacheRemoveSlot(FrameCacheSlot::Next);
    Preferences prefs;
    if (prefs.begin("tmads_campaign", false)) {
        prefs.remove("active_hash");
        prefs.end();
    }
}

}  // namespace

SyncOutcome syncOnWake(const char *wakeReason) {
    if (!clientBegin()) {
        Serial.println("TMAds: client init failed");
        return SyncOutcome::Failed;
    }
    const bool afterOta = identityConsumedOtaRebootFlag();
    const CheckInResult checkIn =
        clientCheckIn(afterOta ? "post-OTA" : wakeReason);
    if (!checkIn.ok) {
        Serial.printf("TMAds: check-in failed: %s\n", checkIn.error.c_str());
        return SyncOutcome::Failed;
    }
    Serial.printf(
        "TMAds: check-in ok (wake=%us awake=%us poll=%us orientation=%d)\n",
        checkIn.wakeIntervalSec, checkIn.awakeTimeoutSec, checkIn.awakePollSec,
        checkIn.displayOrientation);

    // The check-in reports the previous completed cycle. Reset only after it
    // succeeds, preserving the just-measured check-in duration for this cycle.
    timingsResetCycle();
    applyServerOrientation(checkIn.displayOrientation);

    const BatteryTelemetry battery = batteryGetCached();
    const bool lowBattery =
        battery.available && battery.socPct >= 0 && battery.socPct < 15;
    if (!afterOta && checkIn.firmware.available && !lowBattery) {
        Serial.printf("TMAds: firmware offer %s (%u bytes)\n",
                      checkIn.firmware.version.c_str(),
                      static_cast<unsigned>(checkIn.firmware.byteLength));
        String error;
        if (clientDownloadFirmware(checkIn.firmware, &error)) {
            return SyncOutcome::FirmwareUpdated;
        }
        Serial.printf("TMAds: firmware update failed: %s\n", error.c_str());
    } else if (!afterOta && checkIn.firmware.available && lowBattery) {
        Serial.printf(
            "TMAds: firmware update deferred at low battery (%d%%)\n",
            battery.socPct);
    }

    const CampaignSyncResult campaigns = runCampaignSync();
    if (!campaigns.ok) {
        Serial.printf("TMAds: campaign sync failed: %s\n",
                      campaigns.error.c_str());
        return SyncOutcome::Failed;
    }
    Serial.printf(
        "TMAds: campaign sync ok (%u catalog, %u cached, %u downloaded)%s%s\n",
        static_cast<unsigned>(campaigns.assetCount),
        static_cast<unsigned>(campaigns.cachedCount),
        static_cast<unsigned>(campaigns.downloadedCount),
        campaigns.assetErrors.length() ? " partial: " : "",
        campaigns.assetErrors.c_str());
    return campaigns.displayChanged ? SyncOutcome::CampaignUpdated
                                    : SyncOutcome::CheckedIn;
}

}  // namespace tmads
