#pragma once

#include <Arduino.h>

namespace tmads {

enum class SyncOutcome {
    None,
    CheckedIn,
    FirmwareUpdated,
    CampaignUpdated,
    Failed,
};

SyncOutcome syncOnWake(const char *wakeReason);

}  // namespace tmads
