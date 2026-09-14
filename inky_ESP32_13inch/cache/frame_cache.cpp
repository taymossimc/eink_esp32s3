#include "frame_cache.h"

#include <LittleFS.h>
#include <string.h>

namespace {

constexpr char kCacheDir[] = "/cache";
constexpr char kCurrentPath[] = "/cache/current.frame";
constexpr char kNextPath[] = "/cache/next.frame";
constexpr char kTxnPath[] = "/cache/frame.txn";
constexpr uint32_t kMagic = 0x45494E4B;
constexpr uint16_t kVersion = 2;
constexpr uint16_t kLegacyVersion = 1;
constexpr uint16_t kWidth = 1200;
constexpr uint16_t kHeight = 1600;

#pragma pack(push, 1)
struct LegacyHeader {
    uint32_t magic;
    uint16_t version;
    uint16_t width;
    uint16_t height;
    uint16_t reserved;
    uint32_t payloadLength;
    uint32_t crc32;
    char filename[96];
};

struct FrameHeader {
    uint32_t magic;
    uint16_t version;
    uint16_t width;
    uint16_t height;
    uint16_t orientation;
    uint32_t payloadLength;
    uint32_t crc32;
    char filename[96];
    char origin[16];
};
#pragma pack(pop)

bool gMounted = false;

const char *slotPath(FrameCacheSlot slot) {
    return slot == FrameCacheSlot::Current ? kCurrentPath : kNextPath;
}

String tempPath(FrameCacheSlot slot) {
    return String(slotPath(slot)) + ".new";
}

String backupPath(FrameCacheSlot slot) {
    return String(slotPath(slot)) + ".bak";
}

uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t length) {
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
        }
    }
    return crc;
}

uint32_t frameCrc32(const uint8_t *frame, size_t length) {
    return crc32Update(0xFFFFFFFFU, frame, length) ^ 0xFFFFFFFFU;
}

bool readHeader(const char *path, FrameHeader *header, size_t *headerSize) {
    if (!gMounted || !header || !LittleFS.exists(path)) {
        return false;
    }
    File file = LittleFS.open(path, FILE_READ);
    if (!file) {
        return false;
    }

    uint32_t magic = 0;
    uint16_t version = 0;
    if (file.read(reinterpret_cast<uint8_t *>(&magic), sizeof(magic)) !=
            sizeof(magic) ||
        file.read(reinterpret_cast<uint8_t *>(&version), sizeof(version)) !=
            sizeof(version)) {
        file.close();
        return false;
    }
    file.seek(0);
    memset(header, 0, sizeof(*header));

    size_t size = 0;
    if (magic == kMagic && version == kVersion) {
        size = sizeof(FrameHeader);
        if (file.read(reinterpret_cast<uint8_t *>(header), size) != size) {
            file.close();
            return false;
        }
    } else if (magic == kMagic && version == kLegacyVersion) {
        LegacyHeader legacy = {};
        size = sizeof(LegacyHeader);
        if (file.read(reinterpret_cast<uint8_t *>(&legacy), size) != size) {
            file.close();
            return false;
        }
        header->magic = legacy.magic;
        header->version = legacy.version;
        header->width = legacy.width;
        header->height = legacy.height;
        header->orientation = 0;
        header->payloadLength = legacy.payloadLength;
        header->crc32 = legacy.crc32;
        strncpy(header->filename, legacy.filename,
                sizeof(header->filename) - 1);
        strncpy(header->origin, "legacy", sizeof(header->origin) - 1);
    } else {
        file.close();
        return false;
    }

    const size_t fileSize = file.size();
    file.close();
    const bool valid =
        header->width == kWidth && header->height == kHeight &&
        header->payloadLength == FRAME_CACHE_PAYLOAD_BYTES &&
        header->filename[0] != '\0' &&
        fileSize == size + header->payloadLength;
    if (valid && headerSize) {
        *headerSize = size;
    }
    return valid;
}

void fillInfo(const FrameHeader &header, FrameCacheInfo *info) {
    if (!info) {
        return;
    }
    *info = {};
    info->available = true;
    info->orientation = header.orientation;
    info->crc32 = header.crc32;
    info->payloadLength = header.payloadLength;
    strncpy(info->filename, header.filename, sizeof(info->filename) - 1);
    strncpy(info->origin, header.origin, sizeof(info->origin) - 1);
}

void recoverAtomicFile(FrameCacheSlot slot) {
    const char *path = slotPath(slot);
    const String temp = tempPath(slot);
    const String backup = backupPath(slot);
    if (!LittleFS.exists(path) && LittleFS.exists(backup)) {
        LittleFS.rename(backup, path);
    }
    if (LittleFS.exists(temp)) {
        LittleFS.remove(temp);
    }
    if (LittleFS.exists(path) && LittleFS.exists(backup)) {
        LittleFS.remove(backup);
    }
}

void recoverSwap() {
    if (!LittleFS.exists(kTxnPath)) {
        return;
    }
    String mode;
    File txn = LittleFS.open(kTxnPath, FILE_READ);
    if (txn) {
        mode = txn.readString();
        txn.close();
    }
    const String oldCurrent = String(kCurrentPath) + ".swap";
    if (!LittleFS.exists(kCurrentPath) && LittleFS.exists(oldCurrent)) {
        LittleFS.rename(oldCurrent, kCurrentPath);
    } else if (LittleFS.exists(kCurrentPath) && LittleFS.exists(oldCurrent)) {
        if (mode == "swap" && !LittleFS.exists(kNextPath)) {
            LittleFS.rename(oldCurrent, kNextPath);
        } else {
            LittleFS.remove(oldCurrent);
        }
    }
    recoverAtomicFile(FrameCacheSlot::Current);
    recoverAtomicFile(FrameCacheSlot::Next);
    LittleFS.remove(kTxnPath);
}

}  // namespace

bool frameCacheBegin() {
    if (gMounted) {
        return true;
    }
    if (!LittleFS.begin(true, "/littlefs", 4, "spiffs")) {
        Serial.println("Cache: LittleFS mount failed");
        return false;
    }
    gMounted = true;
    if (!LittleFS.exists(kCacheDir)) {
        LittleFS.mkdir(kCacheDir);
    }
    recoverAtomicFile(FrameCacheSlot::Current);
    recoverAtomicFile(FrameCacheSlot::Next);
    recoverSwap();

    Serial.printf("Cache: LittleFS mounted (%u/%u bytes used), current=%s next=%s\n",
                  static_cast<unsigned>(LittleFS.usedBytes()),
                  static_cast<unsigned>(LittleFS.totalBytes()),
                  frameCacheSlotAvailable(FrameCacheSlot::Current) ? "ready"
                                                                  : "empty",
                  frameCacheSlotAvailable(FrameCacheSlot::Next) ? "ready"
                                                               : "empty");
    return true;
}

bool frameCacheGetInfo(FrameCacheSlot slot, FrameCacheInfo *info) {
    if (!info) {
        return false;
    }
    *info = {};
    FrameHeader header = {};
    if (!readHeader(slotPath(slot), &header, nullptr)) {
        return false;
    }
    fillInfo(header, info);
    return true;
}

bool frameCacheSlotAvailable(FrameCacheSlot slot) {
    FrameHeader header = {};
    return readHeader(slotPath(slot), &header, nullptr);
}

bool frameCacheRemoveSlot(FrameCacheSlot slot) {
    if (!gMounted) return false;
    const char *path = slotPath(slot);
    return !LittleFS.exists(path) || LittleFS.remove(path);
}

bool frameCacheLoadSlot(FrameCacheSlot slot, uint8_t *frame, size_t capacity,
                        FrameCacheInfo *info) {
    if (!frame || capacity < FRAME_CACHE_PAYLOAD_BYTES) {
        return false;
    }
    FrameHeader header = {};
    size_t headerSize = 0;
    const char *path = slotPath(slot);
    if (!readHeader(path, &header, &headerSize)) {
        return false;
    }
    File file = LittleFS.open(path, FILE_READ);
    if (!file || !file.seek(headerSize)) {
        if (file) file.close();
        return false;
    }
    size_t total = 0;
    uint32_t crc = 0xFFFFFFFFU;
    while (total < header.payloadLength) {
        const size_t request =
            min(static_cast<size_t>(8192),
                static_cast<size_t>(header.payloadLength) - total);
        const size_t got = file.read(frame + total, request);
        if (!got) break;
        crc = crc32Update(crc, frame + total, got);
        total += got;
        delay(0);
    }
    file.close();
    crc ^= 0xFFFFFFFFU;
    if (total != header.payloadLength || crc != header.crc32) {
        Serial.printf("Cache: validation failed for %s\n", path);
        return false;
    }
    fillInfo(header, info);
    return true;
}

bool frameCacheStoreSlot(FrameCacheSlot slot, const uint8_t *frame,
                         size_t length, const char *filename,
                         uint16_t orientation, const char *origin) {
    if (!gMounted || !frame || length != FRAME_CACHE_PAYLOAD_BYTES ||
        !filename || !filename[0]) {
        return false;
    }
    FrameHeader header = {};
    header.magic = kMagic;
    header.version = kVersion;
    header.width = kWidth;
    header.height = kHeight;
    header.orientation = orientation;
    header.payloadLength = length;
    header.crc32 = frameCrc32(frame, length);
    strncpy(header.filename, filename, sizeof(header.filename) - 1);
    strncpy(header.origin, origin ? origin : "unknown",
            sizeof(header.origin) - 1);

    const char *path = slotPath(slot);
    const String temp = tempPath(slot);
    const String backup = backupPath(slot);
    LittleFS.remove(temp);
    File file = LittleFS.open(temp, FILE_WRITE);
    if (!file) return false;
    bool ok = file.write(reinterpret_cast<const uint8_t *>(&header),
                         sizeof(header)) == sizeof(header);
    size_t written = 0;
    while (ok && written < length) {
        const size_t chunk = min(static_cast<size_t>(8192), length - written);
        const size_t got = file.write(frame + written, chunk);
        ok = got == chunk;
        written += got;
        delay(0);
    }
    file.flush();
    file.close();
    if (!ok || written != length) {
        LittleFS.remove(temp);
        return false;
    }

    LittleFS.remove(backup);
    const bool hadPrevious = LittleFS.exists(path);
    if (hadPrevious && !LittleFS.rename(path, backup)) {
        LittleFS.remove(temp);
        return false;
    }
    if (!LittleFS.rename(temp, path)) {
        if (hadPrevious) LittleFS.rename(backup, path);
        LittleFS.remove(temp);
        return false;
    }
    LittleFS.remove(backup);
    Serial.printf("Cache: stored %s in %s (%u bytes, crc=%08X)\n",
                  filename,
                  slot == FrameCacheSlot::Current ? "current" : "next",
                  static_cast<unsigned>(length),
                  static_cast<unsigned>(header.crc32));
    return true;
}

bool frameCachePromoteNext(bool queuePrevious) {
    if (!gMounted || !frameCacheSlotAvailable(FrameCacheSlot::Next)) {
        return false;
    }
    File txn = LittleFS.open(kTxnPath, FILE_WRITE);
    if (!txn) return false;
    txn.print(queuePrevious ? "swap" : "promote");
    txn.flush();
    txn.close();

    const String oldCurrent = String(kCurrentPath) + ".swap";
    LittleFS.remove(oldCurrent);
    const bool hadCurrent = LittleFS.exists(kCurrentPath);
    if (hadCurrent && !LittleFS.rename(kCurrentPath, oldCurrent)) {
        LittleFS.remove(kTxnPath);
        return false;
    }
    if (!LittleFS.rename(kNextPath, kCurrentPath)) {
        if (hadCurrent) LittleFS.rename(oldCurrent, kCurrentPath);
        LittleFS.remove(kTxnPath);
        return false;
    }
    if (queuePrevious && hadCurrent) {
        LittleFS.remove(kNextPath);
        if (!LittleFS.rename(oldCurrent, kNextPath)) {
            Serial.println("Cache: current promoted but previous frame not queued");
        }
    } else {
        LittleFS.remove(oldCurrent);
    }
    LittleFS.remove(kTxnPath);
    return true;
}

bool frameCacheLoad(uint8_t *frame, size_t capacity,
                    char *filename, size_t filenameCapacity) {
    if (!filename || filenameCapacity == 0) return false;
    FrameCacheInfo info = {};
    if (!frameCacheLoadSlot(FrameCacheSlot::Next, frame, capacity, &info)) {
        return false;
    }
    strncpy(filename, info.filename, filenameCapacity - 1);
    filename[filenameCapacity - 1] = '\0';
    return true;
}

bool frameCacheStore(const uint8_t *frame, size_t length,
                     const char *filename) {
    return frameCacheStoreSlot(FrameCacheSlot::Next, frame, length, filename);
}

void frameCacheConsume() {
    if (gMounted && LittleFS.exists(kNextPath)) {
        LittleFS.remove(kNextPath);
    }
}

bool frameCacheAvailable() {
    return frameCacheSlotAvailable(FrameCacheSlot::Next);
}
