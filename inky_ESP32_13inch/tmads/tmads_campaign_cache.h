#pragma once

#include <Arduino.h>

#include <vector>

namespace tmads {

// Legacy compile-time cap kept only for stack-allocated helpers in the
// portal UI; the cache itself is dynamic.
constexpr size_t kMaxCampaignAssets = 32;

struct CatalogAsset {
    char assetHash[72] = {};     // Original derivative hash (contract identity).
    char renderedHash[72] = {};  // Device JPEG derivative hash (file identity).
    char campaignId[80] = {};
    char mediaId[80] = {};
    char campaignStatus[16] = {};
    char variant[12] = {};       // "portrait" or "landscape".
    uint32_t byteLength = 0;     // Rendered derivative size in bytes.
    bool cached = false;         // Present and hash-verified on storage.
    bool eligible = false;       // Listed in the targeted display manifest.
};

// Kept for portal compatibility.
struct CampaignAsset {
    char assetHash[72] = {};
    char campaignId[80] = {};
    char mediaId[80] = {};
    uint32_t byteLength = 0;
};

struct CampaignCacheStatus {
    bool ready = false;
    char cursor[32] = {};
    size_t assetCount = 0;
    size_t cachedCount = 0;
    size_t usedBytes = 0;    // LittleFS bytes used (framebuffer + fallback).
    size_t totalBytes = 0;   // LittleFS capacity.
    uint64_t usbCampaignBytes = 0;  // Bytes of campaign assets on USB.
    bool usbActive = false;
};

bool campaignCacheBegin();

// Catalog access (loaded from /usb/tmads/catalog.json or LittleFS fallback).
size_t campaignCatalogCount();
bool campaignCatalogGet(size_t index, CatalogAsset *out);
bool campaignCatalogFindByAssetHash(const char *assetHash, CatalogAsset *out);

// Verified rendered-asset storage keyed by the rendered derivative hash.
bool campaignCacheHasRendered(const char *renderedHash);
bool campaignCacheStoreRendered(const char *renderedHash, uint32_t expectedBytes,
                                const uint8_t *data, size_t length);
bool campaignCacheLoadRendered(const char *renderedHash, uint8_t **dataOut,
                               size_t *lengthOut);

// Replace the persisted catalog, refresh cached flags, and garbage-collect
// files that no longer appear in the catalog.
bool campaignCacheCommitCatalog(const char *cursor,
                                const std::vector<CatalogAsset> &assets);

bool campaignCacheGetStatus(CampaignCacheStatus *status);
size_t campaignCacheList(CampaignAsset *assets, size_t capacity);

// SHA-256 helper (returns lowercase hex, 64 chars + NUL).
void campaignSha256Hex(const uint8_t *data, size_t length, char *hexOut65);

}  // namespace tmads
