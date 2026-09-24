#pragma once

#include <Arduino.h>

namespace tmads {

struct FirmwareOffer {
    bool available = false;
    String version;
    String artifactHash;
    String urlPath;
    size_t byteLength = 0;
};

struct CheckInResult {
    bool ok = false;
    String error;
    FirmwareOffer firmware;
    uint32_t wakeIntervalSec = 0;
    uint32_t awakeTimeoutSec = 0;
    uint32_t awakePollSec = 0;
    int displayOrientation = -1;  // -1 when the server sent no orientation.
};

struct CampaignSyncResult {
    bool ok = false;
    bool displayChanged = false;
    size_t assetCount = 0;      // Assets in the preload catalog.
    size_t cachedCount = 0;     // Assets verified on local storage.
    size_t downloadedCount = 0; // Assets downloaded this cycle.
    String error;               // Fatal error (sync aborted).
    String assetErrors;         // Partial per-asset failures (sync continued).
};

bool clientBegin();
bool clientEnrollIfNeeded(String *errorOut = nullptr);
CheckInResult clientCheckIn(const char *wakeReason);
bool clientDownloadFirmware(const FirmwareOffer &offer, String *errorOut = nullptr);
CampaignSyncResult clientSyncCampaigns();

}  // namespace tmads
