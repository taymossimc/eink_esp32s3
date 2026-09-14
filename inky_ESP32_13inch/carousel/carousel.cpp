#include "carousel.h"

#include "cache/frame_cache.h"
#include "carousel_config.h"
#include "config/device_config.h"
#include "display/display_panel.h"
#include "image/image_pipeline.h"
#include "log/phone_home_log.h"
#include "modem/modem.h"
#include "network/network_manager.h"
#include "power/power.h"

#include <ArduinoJson.h>
#include <esp_heap_caps.h>
#include <esp_random.h>
#include <limits.h>
#include <string.h>
#include <time.h>

namespace {

RTC_DATA_ATTR char gCurrentImageName[96] = "";

bool isImageFilename(const char *name) {
    if (!name || name[0] == '.') {
        return false;
    }
    const char *dot = strrchr(name, '.');
    if (!dot) {
        return false;
    }
    const char *ext = dot + 1;
    return strcasecmp(ext, "jpg") == 0 || strcasecmp(ext, "jpeg") == 0 ||
           strcasecmp(ext, "png") == 0;
}

void urlEncodePathSegment(const char *name, char *out, size_t outSize) {
    size_t o = 0;
    for (size_t i = 0; name[i] != '\0' && o + 4 < outSize; ++i) {
        const char c = name[i];
        const bool safe =
            (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
        if (safe) {
            out[o++] = c;
        } else {
            o += snprintf(out + o, outSize - o, "%%%02X", static_cast<uint8_t>(c));
        }
    }
    out[o] = '\0';
}

bool pickRandomFilename(const JsonArray &files, char *outName, size_t outSize) {
    static constexpr int kMaxImages = 64;
    const char *names[kMaxImages] = {};
    int sizes[kMaxImages] = {};
    int imageCount = 0;

    for (JsonObject item : files) {
        const char *type = item["type"] | "";
        const char *name = item["name"] | "";
        if (strcmp(type, "file") == 0 && isImageFilename(name) &&
            imageCount < kMaxImages) {
            names[imageCount] = name;
            sizes[imageCount] = item["size"].as<int>();
            imageCount++;
        }
    }

    if (imageCount == 0) {
        Serial.println("Carousel: no image files in index");
        return false;
    }

    // Prefer the smallest file that is not the current image — large PNGs over
    // LTE SSL can take many minutes on the ESP32.
    int bestIdx = -1;
    int bestSize = INT32_MAX;
    for (int i = 0; i < imageCount; ++i) {
        if (gCurrentImageName[0] != '\0' &&
            strcmp(names[i], gCurrentImageName) == 0) {
            continue;
        }
        const int fileSize = sizes[i] > 0 ? sizes[i] : INT32_MAX / 2;
        if (fileSize < bestSize) {
            bestSize = fileSize;
            bestIdx = i;
        }
    }

    if (bestIdx < 0) {
        bestIdx = static_cast<int>(esp_random() % imageCount);
    }

    strncpy(outName, names[bestIdx], outSize - 1);
    outName[outSize - 1] = '\0';
    if (bestSize > 0 && bestSize < INT32_MAX / 2) {
        Serial.printf("Carousel: smallest candidate is %s (%d bytes)\n",
                      outName, bestSize);
    }
    return true;
}

}  // namespace

const char *carouselCurrentImageName() {
    return gCurrentImageName;
}

namespace {

void logFetch(const char *host, const char *path, const char *transport,
              const char *fallback, bool success, size_t bytes,
              uint32_t durationMs) {
    phone_home_log::Event event = {};
    event.uptimeMs = millis();
    const time_t now = time(nullptr);
    if (now > 1700000000) {
        struct tm utc = {};
        gmtime_r(&now, &utc);
        strftime(event.timestamp, sizeof(event.timestamp),
                 "%Y-%m-%dT%H:%M:%SZ", &utc);
    }
    strncpy(event.trigger, "carousel", sizeof(event.trigger) - 1);
    snprintf(event.endpoint, sizeof(event.endpoint), "https://%s%s", host, path);
    strncpy(event.transport, transport, sizeof(event.transport) - 1);
    strncpy(event.fallbackSummary, fallback,
            sizeof(event.fallbackSummary) - 1);
    strncpy(event.result, success ? "ok" : "request failed",
            sizeof(event.result) - 1);
    event.httpStatus = success ? 200 : 0;
    event.byteCount = static_cast<uint32_t>(bytes);
    event.durationMs = durationMs;
    event.success = success;
    phone_home_log::append(event);
}

bool fetchHttpsWithFallback(const char *host, const char *path,
                            uint8_t **bodyOut, size_t *lenOut,
                            size_t maxBytes) {
    if (networkConnectSaved(12000)) {
        const uint32_t start = millis();
        const bool ok = networkWifiHttpsGet(host, path, bodyOut, lenOut,
                                            maxBytes);
        logFetch(host, path, "wifi", "", ok, ok ? *lenOut : 0,
                 millis() - start);
        if (ok) return true;
    }

    for (int attempt = 0; attempt < 3; ++attempt) {
        if (modemEnsureDataConnected()) break;
        delay(5000);
    }
    if (!modemIsDataConnected()) {
        logFetch(host, path, "cellular", "wifi unavailable", false, 0, 0);
        return false;
    }
    const uint32_t start = millis();
    const bool ok = modemHttpsGet(host, path, bodyOut, lenOut, maxBytes);
    logFetch(host, path, "cellular", "wifi->cellular", ok,
             ok ? *lenOut : 0, millis() - start);
    return ok;
}

bool fetchDecodedFrame(uint8_t **frameOut, char *chosenName,
                       size_t chosenNameSize) {
    if (!frameOut || !chosenName || chosenNameSize == 0) {
        return false;
    }
    *frameOut = nullptr;
    chosenName[0] = '\0';

    Serial.println("\n=== Carousel fetch ===");

    uint8_t *indexBody = nullptr;
    size_t indexLen = 0;
    if (!fetchHttpsWithFallback(CAROUSEL_HOST, CAROUSEL_INDEX_PATH,
                                &indexBody, &indexLen, 65536)) {
        Serial.println("Carousel: index fetch failed");
        return false;
    }

    JsonDocument doc;
    const DeserializationError err =
        deserializeJson(doc, indexBody, indexLen);
    heap_caps_free(indexBody);
    if (err) {
        Serial.printf("Carousel: JSON parse failed: %s\n", err.c_str());
        return false;
    }

    JsonArray files = doc.as<JsonArray>();
    if (!pickRandomFilename(files, chosenName, chosenNameSize)) {
        return false;
    }

    Serial.printf("Carousel: selected %s (current=%s)\n", chosenName,
                  gCurrentImageName[0] ? gCurrentImageName : "(none)");

    char encodedName[160] = {};
    urlEncodePathSegment(chosenName, encodedName, sizeof(encodedName));
    char imagePath[200] = {};
    snprintf(imagePath, sizeof(imagePath), "%s/%s", CAROUSEL_INDEX_PATH,
             encodedName);

    uint8_t *imageBody = nullptr;
    size_t imageLen = 0;
    if (!fetchHttpsWithFallback(CAROUSEL_HOST, imagePath, &imageBody,
                                &imageLen, CAROUSEL_MAX_DOWNLOAD_BYTES)) {
        Serial.println("Carousel: image download failed");
        return false;
    }

    uint8_t *frame =
        static_cast<uint8_t *>(heap_caps_malloc(FRAME_CACHE_PAYLOAD_BYTES,
                                                MALLOC_CAP_SPIRAM));
    if (!frame) {
        frame = static_cast<uint8_t *>(malloc(FRAME_CACHE_PAYLOAD_BYTES));
    }
    if (!frame) {
        Serial.println("Carousel: framebuffer alloc failed");
        heap_caps_free(imageBody);
        return false;
    }

    size_t packedLen = 0;
    const auto config = device_config::getSnapshot();
    const uint16_t orientation =
        static_cast<uint16_t>(config.displayOrientation);
    const bool decoded =
        imageDecodeCropFill(imageBody, imageLen, chosenName, frame,
                            FRAME_CACHE_PAYLOAD_BYTES, &packedLen, orientation);
    heap_caps_free(imageBody);
    if (!decoded || packedLen != FRAME_CACHE_PAYLOAD_BYTES) {
        heap_caps_free(frame);
        return false;
    }

    *frameOut = frame;
    return true;
}

void setCurrentImageName(const char *name) {
    strncpy(gCurrentImageName, name, sizeof(gCurrentImageName) - 1);
    gCurrentImageName[sizeof(gCurrentImageName) - 1] = '\0';
}

}  // namespace

bool carouselCacheBegin() {
    return frameCacheBegin();
}

namespace {

bool displayCached(bool queuePrevious) {
    uint8_t *frame = static_cast<uint8_t *>(
        heap_caps_malloc(FRAME_CACHE_PAYLOAD_BYTES, MALLOC_CAP_SPIRAM));
    if (!frame) {
        frame = static_cast<uint8_t *>(malloc(FRAME_CACHE_PAYLOAD_BYTES));
    }
    if (!frame) {
        Serial.println("Cache: framebuffer allocation failed");
        return false;
    }

    FrameCacheInfo cached = {};
    if (!frameCacheLoadSlot(FrameCacheSlot::Next, frame,
                            FRAME_CACHE_PAYLOAD_BYTES, &cached)) {
        heap_caps_free(frame);
        Serial.println("Cache: no prepared frame");
        return false;
    }
    const uint16_t configuredOrientation = static_cast<uint16_t>(
        device_config::getSnapshot().displayOrientation);
    if (cached.orientation != configuredOrientation) {
        Serial.println("Cache: next frame orientation is stale");
        heap_caps_free(frame);
        frameCacheRemoveSlot(FrameCacheSlot::Next);
        return false;
    }

    Serial.printf("Cache: displaying %s before cellular work\n",
                  cached.filename);
    const bool shown =
        displayShowPackedImage(frame, FRAME_CACHE_PAYLOAD_BYTES);
    if (shown) {
        setCurrentImageName(cached.filename);
        if (!frameCachePromoteNext(queuePrevious)) {
            Serial.println("Cache: display succeeded but slot promotion failed");
        }
        activityBump();
    }

    heap_caps_free(frame);
    return shown;
}

}  // namespace

bool carouselDisplayCached() {
    return displayCached(false);
}

bool carouselDisplayCachedAndSwap() {
    return displayCached(true);
}

bool carouselFetchAndCache() {
    uint8_t *frame = nullptr;
    char chosenName[96] = {};
    if (!fetchDecodedFrame(&frame, chosenName, sizeof(chosenName))) {
        return false;
    }

    const bool stored =
        frameCacheStoreSlot(FrameCacheSlot::Next, frame,
                            FRAME_CACHE_PAYLOAD_BYTES, chosenName,
                            static_cast<uint16_t>(
                                device_config::getSnapshot().displayOrientation),
                            "download");
    heap_caps_free(frame);
    return stored;
}

bool carouselFetchAndDisplay() {
    uint8_t *frame = nullptr;
    char chosenName[96] = {};
    if (!fetchDecodedFrame(&frame, chosenName, sizeof(chosenName))) {
        return false;
    }

    const bool shown =
        displayShowPackedImage(frame, FRAME_CACHE_PAYLOAD_BYTES);
    if (shown) {
        if (!frameCacheStoreSlot(FrameCacheSlot::Current, frame,
                                 FRAME_CACHE_PAYLOAD_BYTES, chosenName,
                                 static_cast<uint16_t>(
                                     device_config::getSnapshot()
                                         .displayOrientation),
                                 "download")) {
            Serial.println("Cache: unable to persist displayed current frame");
        }
        setCurrentImageName(chosenName);
        activityBump();
    }

    heap_caps_free(frame);
    return shown;
}
