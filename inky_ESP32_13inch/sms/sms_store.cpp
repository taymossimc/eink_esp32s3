#include "sms_store.h"

#include <LittleFS.h>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <string.h>

namespace {

constexpr char kDir[] = "/sms";
constexpr char kPath[] = "/sms/inbox.bin";
constexpr char kTempPath[] = "/sms/inbox.new";
constexpr char kBackupPath[] = "/sms/inbox.bak";
constexpr uint32_t kMagic = 0x534D5349;  // SMSI
constexpr uint16_t kVersion = 1;

#pragma pack(push, 1)
struct StoreHeader {
    uint32_t magic;
    uint16_t version;
    uint16_t count;
    uint32_t crc32;
};
#pragma pack(pop)

SmsMessage *gMessages = nullptr;
size_t gCount = 0;
bool gReady = false;
SemaphoreHandle_t gMutex = nullptr;

uint32_t crcUpdate(uint32_t crc, const uint8_t *data, size_t length) {
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
        }
    }
    return crc;
}

uint32_t recordsCrc(const SmsMessage *messages, size_t count) {
    return crcUpdate(0xFFFFFFFFU,
                     reinterpret_cast<const uint8_t *>(messages),
                     count * sizeof(SmsMessage)) ^
           0xFFFFFFFFU;
}

bool saveLocked() {
    StoreHeader header = {kMagic, kVersion, static_cast<uint16_t>(gCount),
                          recordsCrc(gMessages, gCount)};
    LittleFS.remove(kTempPath);
    File file = LittleFS.open(kTempPath, FILE_WRITE);
    if (!file) return false;
    bool ok = file.write(reinterpret_cast<const uint8_t *>(&header),
                         sizeof(header)) == sizeof(header);
    if (ok && gCount) {
        ok = file.write(reinterpret_cast<const uint8_t *>(gMessages),
                        gCount * sizeof(SmsMessage)) ==
             gCount * sizeof(SmsMessage);
    }
    file.flush();
    file.close();
    if (!ok) {
        LittleFS.remove(kTempPath);
        return false;
    }
    LittleFS.remove(kBackupPath);
    if (LittleFS.exists(kPath) && !LittleFS.rename(kPath, kBackupPath)) {
        LittleFS.remove(kTempPath);
        return false;
    }
    if (!LittleFS.rename(kTempPath, kPath)) {
        if (LittleFS.exists(kBackupPath)) {
            LittleFS.rename(kBackupPath, kPath);
        }
        return false;
    }
    LittleFS.remove(kBackupPath);
    return true;
}

bool loadFile(const char *path) {
    File file = LittleFS.open(path, FILE_READ);
    if (!file) return false;
    StoreHeader header = {};
    bool ok = file.read(reinterpret_cast<uint8_t *>(&header),
                        sizeof(header)) == sizeof(header);
    ok = ok && header.magic == kMagic && header.version == kVersion &&
         header.count <= SMS_STORE_MAX_MESSAGES &&
         file.size() == sizeof(header) +
                            static_cast<size_t>(header.count) *
                                sizeof(SmsMessage);
    if (ok && header.count) {
        ok = file.read(reinterpret_cast<uint8_t *>(gMessages),
                       static_cast<size_t>(header.count) *
                           sizeof(SmsMessage)) ==
             static_cast<size_t>(header.count) * sizeof(SmsMessage);
    }
    file.close();
    if (!ok ||
        recordsCrc(gMessages, header.count) != header.crc32) {
        return false;
    }
    gCount = header.count;
    return true;
}

class Lock {
  public:
    Lock() { if (gMutex) xSemaphoreTake(gMutex, portMAX_DELAY); }
    ~Lock() { if (gMutex) xSemaphoreGive(gMutex); }
};

}  // namespace

uint32_t smsMessageFingerprint(const SmsMessage &message) {
    uint32_t crc = 0xFFFFFFFFU;
    crc = crcUpdate(crc, reinterpret_cast<const uint8_t *>(message.sender),
                    strnlen(message.sender, sizeof(message.sender)));
    crc = crcUpdate(crc, reinterpret_cast<const uint8_t *>(message.timestamp),
                    strnlen(message.timestamp, sizeof(message.timestamp)));
    crc = crcUpdate(crc, reinterpret_cast<const uint8_t *>(message.body),
                    strnlen(message.body, sizeof(message.body)));
    return crc ^ 0xFFFFFFFFU;
}

bool smsStoreBegin() {
    if (gReady) return true;
    if (!gMutex) gMutex = xSemaphoreCreateMutex();
    if (!gMutex) return false;
    if (!gMessages) {
        gMessages = static_cast<SmsMessage *>(
            heap_caps_calloc(SMS_STORE_MAX_MESSAGES, sizeof(SmsMessage),
                             MALLOC_CAP_SPIRAM));
    }
    if (!gMessages) return false;
    if (!LittleFS.exists(kDir)) LittleFS.mkdir(kDir);
    if (!LittleFS.exists(kPath) && LittleFS.exists(kBackupPath)) {
        LittleFS.rename(kBackupPath, kPath);
    }
    if (LittleFS.exists(kTempPath)) LittleFS.remove(kTempPath);
    gCount = 0;
    if (LittleFS.exists(kPath) && !loadFile(kPath)) {
        Serial.println("SMS store: corrupt inbox moved aside");
        LittleFS.rename(kPath, "/sms/inbox.corrupt");
        gCount = 0;
    }
    gReady = true;
    Serial.printf("SMS store: %u archived message(s)\n",
                  static_cast<unsigned>(gCount));
    return true;
}

size_t smsStoreCount() {
    Lock lock;
    return gCount;
}

bool smsStoreAppend(const SmsMessage &message) {
    if (!gReady) return false;
    Lock lock;
    SmsMessage copy = message;
    copy.sender[sizeof(copy.sender) - 1] = '\0';
    copy.timestamp[sizeof(copy.timestamp) - 1] = '\0';
    copy.body[sizeof(copy.body) - 1] = '\0';
    copy.id = smsMessageFingerprint(copy);
    for (size_t i = 0; i < gCount; ++i) {
        if (gMessages[i].id == copy.id) return true;
    }
    if (gCount == SMS_STORE_MAX_MESSAGES) {
        memmove(gMessages, gMessages + 1,
                (gCount - 1) * sizeof(SmsMessage));
        --gCount;
    }
    gMessages[gCount++] = copy;
    if (!saveLocked()) {
        --gCount;
        return false;
    }
    return true;
}

bool smsStoreGetNewest(size_t newestIndex, SmsMessage *message) {
    if (!gReady || !message) return false;
    Lock lock;
    if (newestIndex >= gCount) return false;
    *message = gMessages[gCount - 1 - newestIndex];
    return true;
}

bool smsStoreRemove(uint32_t id) {
    if (!gReady) return false;
    Lock lock;
    for (size_t i = 0; i < gCount; ++i) {
        if (gMessages[i].id != id) continue;
        memmove(gMessages + i, gMessages + i + 1,
                (gCount - i - 1) * sizeof(SmsMessage));
        --gCount;
        return saveLocked();
    }
    return false;
}

bool smsStoreClear() {
    if (!gReady) return false;
    Lock lock;
    gCount = 0;
    return saveLocked();
}
