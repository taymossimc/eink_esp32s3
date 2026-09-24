#include "tmads_timing.h"

namespace tmads {
namespace {
SyncTimings gTimings;
}

SyncTimings &timings() { return gTimings; }

void timingsResetCycle() {
    const uint32_t boot = gTimings.bootToSyncMs;
    const uint32_t connect = gTimings.connectMs;
    const uint32_t checkIn = gTimings.checkInMs;
    gTimings = SyncTimings{};
    gTimings.bootToSyncMs = boot;
    gTimings.connectMs = connect;
    gTimings.checkInMs = checkIn;
}

}  // namespace tmads
