#pragma once

#include <Arduino.h>

constexpr size_t FRAME_CACHE_PAYLOAD_BYTES = 1200U * 1600U / 2U;

enum class FrameCacheSlot : uint8_t {
    Current = 0,
    Next = 1,
};

struct FrameCacheInfo {
    bool available = false;
    uint16_t orientation = 0;
    uint32_t crc32 = 0;
    uint32_t payloadLength = 0;
    char filename[96] = {};
    char origin[16] = {};
};

// Mount and recover the persistent LittleFS cache.
bool frameCacheBegin();

bool frameCacheLoadSlot(FrameCacheSlot slot, uint8_t *frame, size_t capacity,
                        FrameCacheInfo *info = nullptr);
bool frameCacheStoreSlot(FrameCacheSlot slot, const uint8_t *frame,
                         size_t length, const char *filename,
                         uint16_t orientation = 0,
                         const char *origin = "download");
bool frameCacheGetInfo(FrameCacheSlot slot, FrameCacheInfo *info);
bool frameCacheSlotAvailable(FrameCacheSlot slot);
bool frameCacheRemoveSlot(FrameCacheSlot slot);

// Promote next to current. When queuePrevious is true, the old current frame
// becomes next; otherwise it is discarded so a fresh prefetch can replace it.
bool frameCachePromoteNext(bool queuePrevious);

// Load and validate the prepared next frame.
bool frameCacheLoad(uint8_t *frame, size_t capacity,
                    char *filename, size_t filenameCapacity);

// Atomically store a validated next frame.
bool frameCacheStore(const uint8_t *frame, size_t length,
                     const char *filename);

// Legacy helper: remove only the next frame.
void frameCacheConsume();

bool frameCacheAvailable();
