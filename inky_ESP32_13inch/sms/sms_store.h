#pragma once

#include <Arduino.h>

constexpr size_t SMS_STORE_MAX_MESSAGES = 100;
constexpr size_t SMS_STORE_BODY_BYTES = 1024;

struct SmsMessage {
    uint32_t id = 0;
    char sender[32] = {};
    char timestamp[32] = {};
    char body[SMS_STORE_BODY_BYTES] = {};
    bool incomplete = false;
};

bool smsStoreBegin();
size_t smsStoreCount();
bool smsStoreAppend(const SmsMessage &message);
bool smsStoreGetNewest(size_t newestIndex, SmsMessage *message);
bool smsStoreRemove(uint32_t id);
bool smsStoreClear();

// Stable fingerprint used to deduplicate modem imports.
uint32_t smsMessageFingerprint(const SmsMessage &message);
