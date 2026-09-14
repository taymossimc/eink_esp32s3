#include "phone_home_log.h"

#include <LittleFS.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <stddef.h>
#include <string.h>

namespace phone_home_log {
namespace {

constexpr char kDirectory[] = "/logs";
constexpr char kLogPath[] = "/logs/phone_home.bin";
constexpr char kTempPath[] = "/logs/phone_home.tmp";
constexpr char kBackupPath[] = "/logs/phone_home.bak";
constexpr uint32_t kRecordMagic = 0x50484C47U;  // "PHLG"
constexpr uint16_t kRecordVersion = 1;

#pragma pack(push, 1)
struct DiskRecord {
    uint32_t magic;
    uint16_t version;
    uint16_t recordSize;
    uint64_t sequence;
    uint64_t uptimeMs;
    int32_t httpStatus;
    uint32_t byteCount;
    uint32_t durationMs;
    uint8_t success;
    uint8_t reserved[3];
    char id[kIdCapacity];
    char timestamp[kTimestampCapacity];
    char trigger[kTriggerCapacity];
    char endpoint[kEndpointCapacity];
    char transport[kTransportCapacity];
    char fallbackSummary[kFallbackSummaryCapacity];
    char result[kResultCapacity];
    uint32_t crc32;
};
#pragma pack(pop)

struct Slot {
    uint64_t sequence;
    uint32_t offset;
};

struct ScanMeta {
    size_t fileSize;
    size_t validTotal;
    size_t invalidTotal;
    bool trailingBytes;
};

SemaphoreHandle_t gMutex = nullptr;
bool gReady = false;
Slot gSlots[kMaxEvents] = {};
size_t gSlotCount = 0;
uint64_t gNextSequence = 1;

class LockGuard {
  public:
    explicit LockGuard(SemaphoreHandle_t mutex)
        : mutex_(mutex),
          locked_(mutex_ && xSemaphoreTakeRecursive(mutex_, portMAX_DELAY) == pdTRUE) {}

    ~LockGuard() {
        if (locked_) {
            xSemaphoreGiveRecursive(mutex_);
        }
    }

    bool locked() const { return locked_; }

  private:
    SemaphoreHandle_t mutex_;
    bool locked_;
};

template <size_t N>
void copyText(char (&destination)[N], const char *source) {
    if (!source) {
        destination[0] = '\0';
        return;
    }
    strncpy(destination, source, N - 1);
    destination[N - 1] = '\0';
}

uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t length) {
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
        }
    }
    return crc;
}

uint32_t recordCrc(const DiskRecord &record) {
    return crc32Update(0xFFFFFFFFU,
                       reinterpret_cast<const uint8_t *>(&record),
                       offsetof(DiskRecord, crc32)) ^
           0xFFFFFFFFU;
}

bool validRecord(const DiskRecord &record) {
    return record.magic == kRecordMagic &&
           record.version == kRecordVersion &&
           record.recordSize == sizeof(DiskRecord) &&
           record.sequence != 0 && record.id[0] != '\0' &&
           record.id[kIdCapacity - 1] == '\0' &&
           record.timestamp[kTimestampCapacity - 1] == '\0' &&
           record.trigger[kTriggerCapacity - 1] == '\0' &&
           record.endpoint[kEndpointCapacity - 1] == '\0' &&
           record.transport[kTransportCapacity - 1] == '\0' &&
           record.fallbackSummary[kFallbackSummaryCapacity - 1] == '\0' &&
           record.result[kResultCapacity - 1] == '\0' &&
           record.crc32 == recordCrc(record);
}

void sortSlots() {
    for (size_t i = 1; i < gSlotCount; ++i) {
        const Slot value = gSlots[i];
        size_t j = i;
        while (j > 0 && gSlots[j - 1].sequence > value.sequence) {
            gSlots[j] = gSlots[j - 1];
            --j;
        }
        gSlots[j] = value;
    }
}

bool scanMain(ScanMeta &meta) {
    meta = {};
    gSlotCount = 0;
    gNextSequence = 1;

    File file = LittleFS.open(kLogPath, FILE_READ);
    if (!file) {
        return false;
    }

    meta.fileSize = file.size();
    const size_t completeRecords = meta.fileSize / sizeof(DiskRecord);
    meta.trailingBytes = (meta.fileSize % sizeof(DiskRecord)) != 0;

    DiskRecord record = {};
    for (size_t i = 0; i < completeRecords; ++i) {
        const size_t read =
            file.read(reinterpret_cast<uint8_t *>(&record), sizeof(record));
        if (read != sizeof(record)) {
            meta.trailingBytes = true;
            break;
        }
        if (!validRecord(record)) {
            ++meta.invalidTotal;
            continue;
        }

        ++meta.validTotal;
        const Slot slot = {
            record.sequence,
            static_cast<uint32_t>(i * sizeof(DiskRecord)),
        };
        if (gSlotCount < kMaxEvents) {
            gSlots[gSlotCount++] = slot;
        } else {
            sortSlots();
            if (slot.sequence > gSlots[0].sequence) {
                gSlots[0] = slot;
            }
        }
        if (record.sequence >= gNextSequence) {
            gNextSequence = record.sequence + 1;
            if (gNextSequence == 0) {
                gNextSequence = 1;
            }
        }
    }
    file.close();
    sortSlots();
    return true;
}

bool cleanFile(const char *path) {
    File file = LittleFS.open(path, FILE_READ);
    if (!file) {
        return false;
    }
    const size_t size = file.size();
    if (size > kMaxEvents * sizeof(DiskRecord) ||
        size % sizeof(DiskRecord) != 0) {
        file.close();
        return false;
    }

    DiskRecord record = {};
    while (file.available()) {
        if (file.read(reinterpret_cast<uint8_t *>(&record), sizeof(record)) !=
                sizeof(record) ||
            !validRecord(record)) {
            file.close();
            return false;
        }
    }
    file.close();
    return true;
}

bool publishTemp() {
    LittleFS.remove(kBackupPath);
    const bool hadMain = LittleFS.exists(kLogPath);
    if (hadMain && !LittleFS.rename(kLogPath, kBackupPath)) {
        LittleFS.remove(kTempPath);
        return false;
    }
    if (!LittleFS.rename(kTempPath, kLogPath)) {
        if (hadMain) {
            LittleFS.rename(kBackupPath, kLogPath);
        }
        LittleFS.remove(kTempPath);
        return false;
    }
    LittleFS.remove(kBackupPath);
    return true;
}

bool compactMain() {
    LittleFS.remove(kTempPath);
    File source = LittleFS.open(kLogPath, FILE_READ);
    File destination = LittleFS.open(kTempPath, FILE_WRITE);
    if (!source || !destination) {
        if (source) {
            source.close();
        }
        if (destination) {
            destination.close();
        }
        LittleFS.remove(kTempPath);
        return false;
    }

    bool ok = true;
    DiskRecord record = {};
    for (size_t i = 0; ok && i < gSlotCount; ++i) {
        ok = source.seek(gSlots[i].offset) &&
             source.read(reinterpret_cast<uint8_t *>(&record), sizeof(record)) ==
                 sizeof(record) &&
             validRecord(record) &&
             destination.write(reinterpret_cast<const uint8_t *>(&record),
                               sizeof(record)) == sizeof(record);
    }
    destination.flush();
    source.close();
    destination.close();

    if (!ok || !cleanFile(kTempPath)) {
        LittleFS.remove(kTempPath);
        return false;
    }
    return publishTemp();
}

bool recoverInterruptedMaintenance() {
    if (LittleFS.exists(kLogPath)) {
        return true;
    }

    if (LittleFS.exists(kTempPath) && cleanFile(kTempPath)) {
        if (LittleFS.rename(kTempPath, kLogPath)) {
            LittleFS.remove(kBackupPath);
            return true;
        }
    }
    LittleFS.remove(kTempPath);

    if (LittleFS.exists(kBackupPath) &&
        LittleFS.rename(kBackupPath, kLogPath)) {
        return true;
    }

    File file = LittleFS.open(kLogPath, FILE_WRITE);
    if (!file) {
        return false;
    }
    file.close();
    return true;
}

bool loadLocked() {
    if (!recoverInterruptedMaintenance()) {
        return false;
    }

    ScanMeta meta = {};
    if (!scanMain(meta)) {
        return false;
    }

    // A main file with no salvageable records may be the interrupted side of
    // a rotation. Prefer its clean backup when one exists.
    if (gSlotCount == 0 && meta.fileSize != 0 &&
        LittleFS.exists(kBackupPath) && cleanFile(kBackupPath)) {
        LittleFS.remove(kLogPath);
        if (!LittleFS.rename(kBackupPath, kLogPath) || !scanMain(meta)) {
            return false;
        }
    }

    const bool needsCompaction =
        meta.trailingBytes || meta.invalidTotal != 0 ||
        meta.validTotal > kMaxEvents ||
        meta.fileSize != gSlotCount * sizeof(DiskRecord);
    if (needsCompaction) {
        if (!compactMain() || !scanMain(meta)) {
            return false;
        }
    }

    if (LittleFS.exists(kTempPath)) LittleFS.remove(kTempPath);
    if (LittleFS.exists(kBackupPath)) LittleFS.remove(kBackupPath);
    gReady = true;
    return true;
}

void diskToEvent(const DiskRecord &record, Event &event) {
    memset(&event, 0, sizeof(event));
    copyText(event.id, record.id);
    copyText(event.timestamp, record.timestamp);
    event.uptimeMs = record.uptimeMs;
    copyText(event.trigger, record.trigger);
    copyText(event.endpoint, record.endpoint);
    copyText(event.transport, record.transport);
    copyText(event.fallbackSummary, record.fallbackSummary);
    copyText(event.result, record.result);
    event.httpStatus = record.httpStatus;
    event.byteCount = record.byteCount;
    event.durationMs = record.durationMs;
    event.success = record.success != 0;
}

void eventToDisk(const Event &event, uint64_t sequence, DiskRecord &record) {
    memset(&record, 0, sizeof(record));
    record.magic = kRecordMagic;
    record.version = kRecordVersion;
    record.recordSize = sizeof(record);
    record.sequence = sequence;
    record.uptimeMs = event.uptimeMs;
    record.httpStatus = event.httpStatus;
    record.byteCount = event.byteCount;
    record.durationMs = event.durationMs;
    record.success = event.success ? 1 : 0;
    copyText(record.id, event.id);
    copyText(record.timestamp, event.timestamp);
    copyText(record.trigger, event.trigger);
    copyText(record.endpoint, event.endpoint);
    copyText(record.transport, event.transport);
    copyText(record.fallbackSummary, event.fallbackSummary);
    copyText(record.result, event.result);
    record.crc32 = recordCrc(record);
}

bool readSlot(size_t ascendingIndex, Event &event) {
    if (ascendingIndex >= gSlotCount) {
        return false;
    }
    File file = LittleFS.open(kLogPath, FILE_READ);
    if (!file || !file.seek(gSlots[ascendingIndex].offset)) {
        if (file) {
            file.close();
        }
        return false;
    }
    DiskRecord record = {};
    const bool ok =
        file.read(reinterpret_cast<uint8_t *>(&record), sizeof(record)) ==
            sizeof(record) &&
        validRecord(record);
    file.close();
    if (ok) {
        diskToEvent(record, event);
    }
    return ok;
}

bool idExists(const char *id) {
    Event existing = {};
    for (size_t i = 0; i < gSlotCount; ++i) {
        if (readSlot(i, existing) && strcmp(existing.id, id) == 0) {
            return true;
        }
    }
    return false;
}

void makeId(uint64_t sequence, char *id, size_t capacity) {
    snprintf(id, capacity, "%08lX-%08lX-%016llX",
             static_cast<unsigned long>(esp_random()),
             static_cast<unsigned long>(esp_random()),
             static_cast<unsigned long long>(sequence));
}

void writeEscaped(Print &output, const char *value) {
    output.write('"');
    const uint8_t *text =
        reinterpret_cast<const uint8_t *>(value ? value : "");
    while (*text) {
        switch (*text) {
            case '"':
                output.print(F("\\\""));
                break;
            case '\\':
                output.print(F("\\\\"));
                break;
            case '\b':
                output.print(F("\\b"));
                break;
            case '\f':
                output.print(F("\\f"));
                break;
            case '\n':
                output.print(F("\\n"));
                break;
            case '\r':
                output.print(F("\\r"));
                break;
            case '\t':
                output.print(F("\\t"));
                break;
            default:
                if (*text < 0x20) {
                    char escaped[7] = {};
                    snprintf(escaped, sizeof(escaped), "\\u%04X", *text);
                    output.print(escaped);
                } else {
                    output.write(*text);
                }
        }
        ++text;
    }
    output.write('"');
}

void writeEventJsonUnlocked(const Event &event, Print &output) {
    output.print(F("{\"id\":"));
    writeEscaped(output, event.id);
    output.print(F(",\"timestamp\":"));
    writeEscaped(output, event.timestamp);
    output.print(F(",\"uptimeMs\":"));
    output.print(static_cast<unsigned long long>(event.uptimeMs));
    output.print(F(",\"trigger\":"));
    writeEscaped(output, event.trigger);
    output.print(F(",\"endpoint\":"));
    writeEscaped(output, event.endpoint);
    output.print(F(",\"transport\":"));
    writeEscaped(output, event.transport);
    output.print(F(",\"fallbackSummary\":"));
    writeEscaped(output, event.fallbackSummary);
    output.print(F(",\"result\":"));
    writeEscaped(output, event.result);
    output.print(F(",\"httpStatus\":"));
    output.print(event.httpStatus);
    output.print(F(",\"byteCount\":"));
    output.print(event.byteCount);
    output.print(F(",\"durationMs\":"));
    output.print(event.durationMs);
    output.print(F(",\"success\":"));
    output.print(event.success ? F("true") : F("false"));
    output.write('}');
}

}  // namespace

bool begin() {
    if (!gMutex) {
        gMutex = xSemaphoreCreateRecursiveMutex();
        if (!gMutex) {
            return false;
        }
    }
    LockGuard lock(gMutex);
    if (!lock.locked()) {
        return false;
    }
    if (gReady) {
        return true;
    }

    // Deliberately do not call LittleFS.begin(); the owning application mounts
    // the "spiffs" partition before this module starts.
    if (!LittleFS.exists(kDirectory) &&
        (!LittleFS.mkdir(kDirectory) || !LittleFS.exists(kDirectory))) {
        return false;
    }
    return loadLocked();
}

bool load() {
    if (!gMutex) {
        return begin();
    }
    LockGuard lock(gMutex);
    if (!lock.locked() || !LittleFS.exists(kDirectory)) {
        return false;
    }
    gReady = false;
    return loadLocked();
}

void sanitizeEndpoint(const char *endpoint, char *sanitized, size_t capacity) {
    if (!sanitized || capacity == 0) {
        return;
    }
    sanitized[0] = '\0';
    if (!endpoint) {
        return;
    }

    const char *end = endpoint;
    const char *sourceLimit = endpoint + kEndpointCapacity - 1;
    while (end < sourceLimit && *end && *end != '?' && *end != '#') {
        ++end;
    }

    const char *authority = nullptr;
    for (const char *cursor = endpoint; cursor + 2 < end; ++cursor) {
        if (cursor[0] == ':' && cursor[1] == '/' && cursor[2] == '/') {
            authority = cursor + 3;
            break;
        }
    }
    if (!authority && end - endpoint >= 2 &&
        endpoint[0] == '/' && endpoint[1] == '/') {
        authority = endpoint + 2;
    }

    const char *userinfoEnd = nullptr;
    if (authority && authority < end) {
        const char *authorityEnd = authority;
        while (authorityEnd < end && *authorityEnd != '/') {
            ++authorityEnd;
        }
        for (const char *cursor = authority; cursor < authorityEnd; ++cursor) {
            if (*cursor == '@') {
                userinfoEnd = cursor + 1;
            }
        }
    }

    size_t written = 0;
    const char *schemeEnd = authority;
    if (userinfoEnd) {
        for (const char *cursor = endpoint;
             cursor < schemeEnd && written + 1 < capacity; ++cursor) {
            sanitized[written++] = *cursor;
        }
        endpoint = userinfoEnd;
    }
    while (endpoint < end && written + 1 < capacity) {
        sanitized[written++] = *endpoint++;
    }
    sanitized[written] = '\0';
}

bool append(const Event &event, Event *storedEvent) {
    if (!gMutex || !gReady) {
        return false;
    }
    LockGuard lock(gMutex);
    if (!lock.locked() || !gReady) {
        return false;
    }

    Event safe = {};
    copyText(safe.id, event.id);
    copyText(safe.timestamp, event.timestamp);
    safe.uptimeMs = event.uptimeMs;
    copyText(safe.trigger, event.trigger);
    sanitizeEndpoint(event.endpoint, safe.endpoint, sizeof(safe.endpoint));
    copyText(safe.transport, event.transport);
    copyText(safe.fallbackSummary, event.fallbackSummary);
    copyText(safe.result, event.result);
    safe.httpStatus = event.httpStatus;
    safe.byteCount = event.byteCount;
    safe.durationMs = event.durationMs;
    safe.success = event.success;

    if (safe.id[0] == '\0' || idExists(safe.id)) {
        makeId(gNextSequence, safe.id, sizeof(safe.id));
    }

    DiskRecord record = {};
    eventToDisk(safe, gNextSequence, record);
    const bool full = gSlotCount == kMaxEvents;
    const uint32_t offset =
        full ? gSlots[0].offset
             : static_cast<uint32_t>(gSlotCount * sizeof(DiskRecord));

    File file = LittleFS.open(kLogPath, full ? "r+" : FILE_APPEND);
    if (!file || !file.seek(offset)) {
        if (file) {
            file.close();
        }
        return false;
    }
    const bool written =
        file.write(reinterpret_cast<const uint8_t *>(&record), sizeof(record)) ==
        sizeof(record);
    file.flush();
    file.close();
    if (!written) {
        return false;
    }

    const Slot newSlot = {gNextSequence, offset};
    if (full) {
        for (size_t i = 1; i < gSlotCount; ++i) {
            gSlots[i - 1] = gSlots[i];
        }
        gSlots[gSlotCount - 1] = newSlot;
    } else {
        gSlots[gSlotCount++] = newSlot;
    }
    ++gNextSequence;
    if (gNextSequence == 0) {
        gNextSequence = 1;
    }
    if (storedEvent) {
        *storedEvent = safe;
    }
    return true;
}

size_t count() {
    if (!gMutex || !gReady) {
        return 0;
    }
    LockGuard lock(gMutex);
    return lock.locked() && gReady ? gSlotCount : 0;
}

size_t readNewest(size_t offset, size_t limit, ReadCallback callback,
                  void *context) {
    if (!gMutex || !gReady || !callback || limit == 0) {
        return 0;
    }
    LockGuard lock(gMutex);
    if (!lock.locked() || !gReady || offset >= gSlotCount) {
        return 0;
    }

    const size_t available = gSlotCount - offset;
    const size_t requested = limit < available ? limit : available;
    size_t delivered = 0;
    Event event = {};
    while (delivered < requested) {
        const size_t ascendingIndex = gSlotCount - 1 - offset - delivered;
        if (!readSlot(ascendingIndex, event) ||
            !callback(event, context)) {
            break;
        }
        ++delivered;
    }
    return delivered;
}

bool getNewest(size_t index, Event &event) {
    if (!gMutex || !gReady) {
        return false;
    }
    LockGuard lock(gMutex);
    return lock.locked() && gReady && index < gSlotCount &&
           readSlot(gSlotCount - 1 - index, event);
}

bool clear() {
    if (!gMutex || !gReady) {
        return false;
    }
    LockGuard lock(gMutex);
    if (!lock.locked() || !gReady) {
        return false;
    }

    LittleFS.remove(kTempPath);
    File empty = LittleFS.open(kTempPath, FILE_WRITE);
    if (!empty) {
        return false;
    }
    empty.flush();
    empty.close();
    if (!publishTemp()) {
        return false;
    }
    gSlotCount = 0;
    gNextSequence = 1;
    return true;
}

bool writeEventJson(const Event &event, Print &output) {
    Event safe = {};
    copyText(safe.id, event.id);
    copyText(safe.timestamp, event.timestamp);
    safe.uptimeMs = event.uptimeMs;
    copyText(safe.trigger, event.trigger);
    sanitizeEndpoint(event.endpoint, safe.endpoint, sizeof(safe.endpoint));
    copyText(safe.transport, event.transport);
    copyText(safe.fallbackSummary, event.fallbackSummary);
    copyText(safe.result, event.result);
    safe.httpStatus = event.httpStatus;
    safe.byteCount = event.byteCount;
    safe.durationMs = event.durationMs;
    safe.success = event.success;
    writeEventJsonUnlocked(safe, output);
    return output.getWriteError() == 0;
}

bool writePageJson(Print &output, size_t offset, size_t limit) {
    if (!gMutex || !gReady) {
        return false;
    }
    LockGuard lock(gMutex);
    if (!lock.locked() || !gReady) {
        return false;
    }

    output.print(F("{\"total\":"));
    output.print(gSlotCount);
    output.print(F(",\"offset\":"));
    output.print(offset);
    output.print(F(",\"limit\":"));
    output.print(limit);
    output.print(F(",\"events\":["));

    const size_t available = offset < gSlotCount ? gSlotCount - offset : 0;
    const size_t requested = limit < available ? limit : available;
    Event event = {};
    bool first = true;
    for (size_t i = 0; i < requested; ++i) {
        if (!readSlot(gSlotCount - 1 - offset - i, event)) {
            return false;
        }
        if (!first) {
            output.write(',');
        }
        first = false;
        writeEventJsonUnlocked(event, output);
    }
    output.print(F("]}"));
    return output.getWriteError() == 0;
}

}  // namespace phone_home_log
