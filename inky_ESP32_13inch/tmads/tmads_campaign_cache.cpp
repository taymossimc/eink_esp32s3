#include "tmads_campaign_cache.h"

#include "storage/usb_campaign_storage.h"

#include <ArduinoJson.h>
#include <LittleFS.h>
#include <dirent.h>
#include <errno.h>
#include <esp_heap_caps.h>
#include <mbedtls/sha256.h>
#include <stdio.h>
#include <sys/stat.h>
#include <string.h>

namespace tmads {
namespace {

constexpr char kLittleDirectory[] = "/campaigns";
constexpr char kLittleCatalog[] = "/campaigns/catalog.json";
constexpr char kUsbRoot[] = "/usb/tmads";
constexpr char kUsbDirectory[] = "/usb/tmads/assets";
constexpr char kUsbCatalog[] = "/usb/tmads/catalog.json";

bool gReady = false;
char gCursor[32] = {};
std::vector<CatalogAsset> gCatalog;

template <size_t N>
void copyText(char (&destination)[N], const char *source) {
    if (!source) {
        destination[0] = '\0';
        return;
    }
    strncpy(destination, source, N - 1);
    destination[N - 1] = '\0';
}

bool validHash(const char *hash) {
    if (!hash || strlen(hash) != 71 || strncmp(hash, "sha256:", 7) != 0) {
        return false;
    }
    for (const char *p = hash + 7; *p; ++p) {
        if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f'))) {
            return false;
        }
    }
    return true;
}

bool usbReady() {
    if (!usbCampaignStorageMounted()) return false;
    mkdir(kUsbRoot, 0775);
    return mkdir(kUsbDirectory, 0775) == 0 || errno == EEXIST;
}

String usbAssetPath(const char *renderedHash) {
    return String(kUsbDirectory) + "/" + String(renderedHash + 7) + ".jpg";
}

String littleAssetPath(const char *renderedHash) {
    return String(kLittleDirectory) + "/" + String(renderedHash + 7) + ".jpg";
}

bool posixFileSize(const String &path, size_t *sizeOut) {
    struct stat info = {};
    if (stat(path.c_str(), &info) != 0 || info.st_size <= 0) return false;
    if (sizeOut) *sizeOut = static_cast<size_t>(info.st_size);
    return true;
}

uint8_t *allocateBuffer(size_t length) {
    uint8_t *data =
        static_cast<uint8_t *>(heap_caps_malloc(length, MALLOC_CAP_SPIRAM));
    if (!data) data = static_cast<uint8_t *>(malloc(length));
    return data;
}

bool readWholeFile(const String &path, uint8_t **dataOut, size_t *lengthOut) {
    FILE *file = fopen(path.c_str(), "rb");
    if (!file) return false;
    fseek(file, 0, SEEK_END);
    const long fileLength = ftell(file);
    rewind(file);
    if (fileLength <= 0) {
        fclose(file);
        return false;
    }
    const size_t length = static_cast<size_t>(fileLength);
    uint8_t *data = allocateBuffer(length);
    if (!data) {
        fclose(file);
        return false;
    }
    const size_t read = fread(data, 1, length, file);
    fclose(file);
    if (read != length) {
        heap_caps_free(data);
        return false;
    }
    *dataOut = data;
    *lengthOut = length;
    return true;
}

bool readLittleFile(const char *path, uint8_t **dataOut, size_t *lengthOut) {
    File file = LittleFS.open(path, "r");
    if (!file || file.size() <= 0) {
        if (file) file.close();
        return false;
    }
    const size_t length = file.size();
    uint8_t *data = allocateBuffer(length);
    if (!data) {
        file.close();
        return false;
    }
    const size_t read = file.read(data, length);
    file.close();
    if (read != length) {
        heap_caps_free(data);
        return false;
    }
    *dataOut = data;
    *lengthOut = length;
    return true;
}

bool hashMatches(const char *renderedHash, const uint8_t *data, size_t length) {
    char hex[65] = {};
    campaignSha256Hex(data, length, hex);
    return strncmp(renderedHash + 7, hex, 64) == 0;
}

bool writeVerifiedPosix(const String &path, const uint8_t *data,
                        size_t length) {
    const String temporary = path + ".tmp";
    remove(temporary.c_str());
    FILE *file = fopen(temporary.c_str(), "wb");
    if (!file) return false;
    const size_t written = fwrite(data, 1, length, file);
    fflush(file);
    fclose(file);
    if (written != length) {
        remove(temporary.c_str());
        return false;
    }
    remove(path.c_str());
    if (rename(temporary.c_str(), path.c_str()) != 0) {
        remove(temporary.c_str());
        return false;
    }
    return true;
}

bool writeVerifiedLittle(const String &path, const uint8_t *data,
                         size_t length) {
    const String temporary = path + ".tmp";
    LittleFS.remove(temporary);
    File file = LittleFS.open(temporary, "w");
    if (!file) return false;
    const size_t written = file.write(data, length);
    file.flush();
    file.close();
    if (written != length) {
        LittleFS.remove(temporary);
        return false;
    }
    LittleFS.remove(path);
    if (!LittleFS.rename(temporary, path)) {
        LittleFS.remove(temporary);
        return false;
    }
    return true;
}

void catalogToJson(JsonDocument &doc) {
    doc["schema"] = "tmads.eink-campaign-catalog-cache.v2";
    doc["cursor"] = gCursor;
    JsonArray output = doc["assets"].to<JsonArray>();
    for (const CatalogAsset &asset : gCatalog) {
        JsonObject item = output.add<JsonObject>();
        item["asset_hash"] = asset.assetHash;
        item["rendered_hash"] = asset.renderedHash;
        item["campaign_id"] = asset.campaignId;
        item["media_id"] = asset.mediaId;
        item["status"] = asset.campaignStatus;
        item["variant"] = asset.variant;
        item["bytes"] = asset.byteLength;
        item["cached"] = asset.cached;
        item["eligible"] = asset.eligible;
    }
}

bool persistCatalog() {
    JsonDocument doc;
    catalogToJson(doc);
    bool stored = false;
    if (usbReady()) {
        FILE *file = fopen((String(kUsbCatalog) + ".tmp").c_str(), "w");
        if (file) {
            String body;
            serializeJson(doc, body);
            const size_t written = fwrite(body.c_str(), 1, body.length(), file);
            fflush(file);
            fclose(file);
            if (written == body.length()) {
                remove(kUsbCatalog);
                stored = rename((String(kUsbCatalog) + ".tmp").c_str(),
                                kUsbCatalog) == 0;
            }
            if (!stored) remove((String(kUsbCatalog) + ".tmp").c_str());
        }
    }
    // Mirror to LittleFS so the catalog survives USB removal.
    const String temporary = String(kLittleCatalog) + ".tmp";
    LittleFS.remove(temporary);
    File file = LittleFS.open(temporary, "w");
    if (file) {
        const bool serialized = serializeJson(doc, file) > 0;
        file.flush();
        file.close();
        if (serialized) {
            LittleFS.remove(kLittleCatalog);
            stored = LittleFS.rename(temporary, kLittleCatalog) || stored;
        } else {
            LittleFS.remove(temporary);
        }
    }
    return stored;
}

bool parseCatalogDocument(JsonDocument &doc) {
    gCursor[0] = '\0';
    gCatalog.clear();
    copyText(gCursor, doc["cursor"] | "");
    for (JsonObjectConst item : doc["assets"].as<JsonArrayConst>()) {
        CatalogAsset asset;
        copyText(asset.assetHash, item["asset_hash"] | "");
        copyText(asset.renderedHash, item["rendered_hash"] | "");
        if (!validHash(asset.assetHash) || !validHash(asset.renderedHash)) {
            continue;
        }
        copyText(asset.campaignId, item["campaign_id"] | "");
        copyText(asset.mediaId, item["media_id"] | "");
        copyText(asset.campaignStatus, item["status"] | "");
        copyText(asset.variant, item["variant"] | "portrait");
        asset.byteLength = item["bytes"] | 0U;
        asset.cached = item["cached"] | false;
        asset.eligible = item["eligible"] | false;
        gCatalog.push_back(asset);
    }
    return true;
}

bool loadCatalog() {
    // USB copy wins; fall back to the LittleFS mirror.
    if (usbCampaignStorageMounted()) {
        uint8_t *data = nullptr;
        size_t length = 0;
        if (readWholeFile(String(kUsbCatalog), &data, &length)) {
            JsonDocument doc;
            const bool ok = !deserializeJson(doc, data, length);
            heap_caps_free(data);
            if (ok) return parseCatalogDocument(doc);
        }
    }
    if (LittleFS.exists(kLittleCatalog)) {
        File file = LittleFS.open(kLittleCatalog, "r");
        if (file) {
            JsonDocument doc;
            const bool ok = !deserializeJson(doc, file);
            file.close();
            if (ok) return parseCatalogDocument(doc);
        }
    }
    gCursor[0] = '\0';
    gCatalog.clear();
    return true;
}

bool renderedFilePresent(const char *renderedHash, size_t *sizeOut) {
    if (usbCampaignStorageMounted() &&
        posixFileSize(usbAssetPath(renderedHash), sizeOut)) {
        return true;
    }
    File file = LittleFS.open(littleAssetPath(renderedHash), "r");
    if (!file) return false;
    const size_t size = file.size();
    file.close();
    if (size == 0) return false;
    if (sizeOut) *sizeOut = size;
    return true;
}

bool catalogReferences(const String &fileName) {
    for (const CatalogAsset &asset : gCatalog) {
        if (fileName.startsWith(String(asset.renderedHash + 7))) return true;
    }
    return false;
}

}  // namespace

void campaignSha256Hex(const uint8_t *data, size_t length, char *hexOut65) {
    uint8_t digest[32] = {};
    mbedtls_sha256_context context;
    mbedtls_sha256_init(&context);
    mbedtls_sha256_starts(&context, 0);
    mbedtls_sha256_update(&context, data, length);
    mbedtls_sha256_finish(&context, digest);
    mbedtls_sha256_free(&context);
    static const char kHex[] = "0123456789abcdef";
    for (size_t i = 0; i < 32; ++i) {
        hexOut65[i * 2] = kHex[digest[i] >> 4];
        hexOut65[i * 2 + 1] = kHex[digest[i] & 0x0F];
    }
    hexOut65[64] = '\0';
}

bool campaignCacheBegin() {
    if (gReady) return true;
    if (!LittleFS.begin(true)) return false;
    if (!LittleFS.exists(kLittleDirectory) &&
        !LittleFS.mkdir(kLittleDirectory)) {
        return false;
    }
    if (!loadCatalog()) {
        gCursor[0] = '\0';
        gCatalog.clear();
    }
    gReady = true;
    return true;
}

size_t campaignCatalogCount() {
    return campaignCacheBegin() ? gCatalog.size() : 0;
}

bool campaignCatalogGet(size_t index, CatalogAsset *out) {
    if (!out || !campaignCacheBegin() || index >= gCatalog.size()) return false;
    *out = gCatalog[index];
    return true;
}

bool campaignCatalogFindByAssetHash(const char *assetHash, CatalogAsset *out) {
    if (!assetHash || !campaignCacheBegin()) return false;
    for (const CatalogAsset &asset : gCatalog) {
        if (strcmp(asset.assetHash, assetHash) == 0) {
            if (out) *out = asset;
            return true;
        }
    }
    return false;
}

bool campaignCacheHasRendered(const char *renderedHash) {
    if (!campaignCacheBegin() || !validHash(renderedHash)) return false;
    // Never skip a download based on filename presence alone. Validate the
    // stored bytes against the advertised rendered hash; the load helper also
    // removes a corrupt object so reconciliation can replace it immediately.
    uint8_t *data = nullptr;
    size_t length = 0;
    const bool valid =
        campaignCacheLoadRendered(renderedHash, &data, &length);
    if (data) heap_caps_free(data);
    return valid;
}

bool campaignCacheStoreRendered(const char *renderedHash,
                                uint32_t expectedBytes, const uint8_t *data,
                                size_t length) {
    if (!campaignCacheBegin() || !validHash(renderedHash) || !data || !length) {
        return false;
    }
    if (expectedBytes != 0 && length != expectedBytes) {
        Serial.printf("Campaign cache: %s length %u != expected %u\n",
                      renderedHash, static_cast<unsigned>(length),
                      static_cast<unsigned>(expectedBytes));
        return false;
    }
    if (!hashMatches(renderedHash, data, length)) {
        Serial.printf("Campaign cache: %s failed SHA-256 verification\n",
                      renderedHash);
        return false;
    }
    if (usbReady() && writeVerifiedPosix(usbAssetPath(renderedHash), data,
                                         length)) {
        LittleFS.remove(littleAssetPath(renderedHash));
        Serial.printf("Campaign cache: stored %s on USB (%u bytes)\n",
                      renderedHash, static_cast<unsigned>(length));
        return true;
    }
    if (writeVerifiedLittle(littleAssetPath(renderedHash), data, length)) {
        Serial.printf("Campaign cache: stored %s on LittleFS (%u bytes)\n",
                      renderedHash, static_cast<unsigned>(length));
        return true;
    }
    return false;
}

bool campaignCacheLoadRendered(const char *renderedHash, uint8_t **dataOut,
                               size_t *lengthOut) {
    if (!dataOut || !lengthOut) return false;
    *dataOut = nullptr;
    *lengthOut = 0;
    if (!campaignCacheBegin() || !validHash(renderedHash)) return false;
    bool loaded = false;
    if (usbCampaignStorageMounted()) {
        loaded = readWholeFile(usbAssetPath(renderedHash), dataOut, lengthOut);
    }
    if (!loaded) {
        loaded = readLittleFile(littleAssetPath(renderedHash).c_str(), dataOut,
                                lengthOut);
    }
    if (!loaded) return false;
    if (!hashMatches(renderedHash, *dataOut, *lengthOut)) {
        Serial.printf(
            "Campaign cache: cached %s is corrupt, discarding\n", renderedHash);
        heap_caps_free(*dataOut);
        *dataOut = nullptr;
        *lengthOut = 0;
        remove(usbAssetPath(renderedHash).c_str());
        LittleFS.remove(littleAssetPath(renderedHash));
        return false;
    }
    return true;
}

bool campaignCacheCommitCatalog(const char *cursor,
                                const std::vector<CatalogAsset> &assets) {
    if (!campaignCacheBegin()) return false;
    copyText(gCursor, cursor ? cursor : "");
    gCatalog = assets;
    for (CatalogAsset &asset : gCatalog) {
        size_t size = 0;
        asset.cached = renderedFilePresent(asset.renderedHash, &size) &&
                       (asset.byteLength == 0 || size == asset.byteLength);
    }
    const bool persisted = persistCatalog();

    // Garbage-collect files no longer referenced by the catalog.
    File directory = LittleFS.open(kLittleDirectory);
    if (directory && directory.isDirectory()) {
        File entry = directory.openNextFile();
        while (entry) {
            const String name = entry.name();
            entry.close();
            if (name.endsWith(".jpg") && !catalogReferences(name)) {
                LittleFS.remove(String(kLittleDirectory) + "/" + name);
                Serial.printf("Campaign cache: removed stale %s\n",
                              name.c_str());
            }
            entry = directory.openNextFile();
        }
        directory.close();
    }
    if (usbCampaignStorageMounted()) {
        DIR *usbDirectory = opendir(kUsbDirectory);
        if (usbDirectory) {
            while (dirent *entry = readdir(usbDirectory)) {
                const String name(entry->d_name);
                if (!name.endsWith(".jpg") &&
                    !name.endsWith(".jpg.tmp")) {
                    continue;
                }
                if (name.endsWith(".jpg") && catalogReferences(name)) continue;
                const String stalePath =
                    String(kUsbDirectory) + "/" + entry->d_name;
                remove(stalePath.c_str());
                Serial.printf("Campaign cache: removed stale USB %s\n",
                              entry->d_name);
            }
            closedir(usbDirectory);
        }
        // Remove the pre-catalog cache directory from earlier firmware.
        DIR *legacy = opendir("/usb/tmads/campaigns");
        if (legacy) {
            while (dirent *entry = readdir(legacy)) {
                const String name(entry->d_name);
                if (name == "." || name == "..") continue;
                remove((String("/usb/tmads/campaigns/") + entry->d_name)
                           .c_str());
            }
            closedir(legacy);
            rmdir("/usb/tmads/campaigns");
        }
    }
    return persisted;
}

bool campaignCacheGetStatus(CampaignCacheStatus *status) {
    if (!status || !campaignCacheBegin()) return false;
    *status = {};
    status->ready = true;
    copyText(status->cursor, gCursor);
    status->assetCount = gCatalog.size();
    for (const CatalogAsset &asset : gCatalog) {
        if (asset.cached) status->cachedCount++;
    }
    status->usedBytes = LittleFS.usedBytes();
    status->totalBytes = LittleFS.totalBytes();
    status->usbActive = usbCampaignStorageMounted();
    if (status->usbActive) {
        DIR *directory = opendir(kUsbDirectory);
        if (directory) {
            while (dirent *entry = readdir(directory)) {
                const String name(entry->d_name);
                if (!name.endsWith(".jpg")) continue;
                size_t size = 0;
                if (posixFileSize(String(kUsbDirectory) + "/" + entry->d_name,
                                  &size)) {
                    status->usbCampaignBytes += size;
                }
            }
            closedir(directory);
        }
    }
    return true;
}

size_t campaignCacheList(CampaignAsset *assets, size_t capacity) {
    if (!assets || !capacity || !campaignCacheBegin()) return 0;
    const size_t count = min(capacity, gCatalog.size());
    for (size_t i = 0; i < count; ++i) {
        copyText(assets[i].assetHash, gCatalog[i].assetHash);
        copyText(assets[i].campaignId, gCatalog[i].campaignId);
        copyText(assets[i].mediaId, gCatalog[i].mediaId);
        assets[i].byteLength = gCatalog[i].byteLength;
    }
    return count;
}

}  // namespace tmads
