#pragma once

#include <Arduino.h>

namespace tmads {

// Phase durations (milliseconds) for the most recent content-change cycle.
// Zero means the phase did not run. Reported with the next check-in.
struct SyncTimings {
    uint32_t bootToSyncMs = 0;       // Power-on until sync start.
    uint32_t connectMs = 0;          // WiFi or cellular bring-up.
    uint32_t checkInMs = 0;          // Signed check-in round trip.
    uint32_t manifestFetchMs = 0;    // Targeted manifest fetch + parse.
    uint32_t catalogFetchMs = 0;     // Preload catalog fetch + parse.
    uint32_t cacheReconcileMs = 0;   // Hash inventory + catalog commit/GC.
    uint32_t downloadMs = 0;         // Asset downloads (all assets).
    uint32_t downloadBytes = 0;      // Total bytes downloaded this cycle.
    uint32_t downloadCount = 0;      // Assets downloaded this cycle.
    uint32_t decodeMs = 0;           // JPEG decode + packing.
    uint32_t displayMs = 0;          // SPI transfer + BUSY refresh.
    uint32_t spiTransferMs = 0;      // Packed framebuffer transfer.
    uint32_t busyRefreshMs = 0;      // Panel BUSY refresh wait.
    uint32_t ackMs = 0;              // Sync acknowledgement round trip.
    uint32_t preSleepReportMs = 0;   // Final status report round trip.
    uint32_t totalMs = 0;            // Whole sync cycle.
    char transport[12] = {};         // "wifi" or "cellular".
    bool displayChanged = false;
    uint32_t completedAtMs = 0;      // millis() when the cycle finished.
};

SyncTimings &timings();
void timingsResetCycle();

}  // namespace tmads
