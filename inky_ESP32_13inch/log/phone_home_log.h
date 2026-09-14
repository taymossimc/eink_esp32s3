#pragma once

#include <Arduino.h>

namespace phone_home_log {

constexpr size_t kMaxEvents = 200;
constexpr size_t kIdCapacity = 40;
constexpr size_t kTimestampCapacity = 32;
constexpr size_t kTriggerCapacity = 32;
constexpr size_t kEndpointCapacity = 192;
constexpr size_t kTransportCapacity = 24;
constexpr size_t kFallbackSummaryCapacity = 96;
constexpr size_t kResultCapacity = 64;

struct Event {
    char id[kIdCapacity];
    char timestamp[kTimestampCapacity];
    uint64_t uptimeMs;
    char trigger[kTriggerCapacity];
    char endpoint[kEndpointCapacity];
    char transport[kTransportCapacity];
    char fallbackSummary[kFallbackSummaryCapacity];
    char result[kResultCapacity];
    int32_t httpStatus;
    uint32_t byteCount;
    uint32_t durationMs;
    bool success;
};

// Called newest-first. Return false to stop the page read early.
using ReadCallback = bool (*)(const Event &event, void *context);

// LittleFS must already be mounted by the application. Creates /logs, recovers
// interrupted maintenance, and loads the persistent index.
bool begin();

// Reload and validate the on-flash log. Normally begin() is sufficient.
bool load();

// Appends one event. Empty or duplicate IDs are replaced with a generated ID.
// The endpoint is always sanitized before persistence.
bool append(const Event &event, Event *storedEvent = nullptr);

size_t count();

// Reads at most limit records newest-first after skipping offset records.
// Returns the number delivered to callback.
size_t readNewest(size_t offset, size_t limit, ReadCallback callback,
                  void *context = nullptr);

bool getNewest(size_t index, Event &event);
bool clear();

// Writes compact portal-ready JSON. writePageJson emits:
// {"total":N,"offset":N,"limit":N,"events":[...]}.
bool writeEventJson(const Event &event, Print &output);
bool writePageJson(Print &output, size_t offset = 0, size_t limit = kMaxEvents);

// Removes query, fragment, and URL user-info credentials.
void sanitizeEndpoint(const char *endpoint, char *sanitized, size_t capacity);

}  // namespace phone_home_log
