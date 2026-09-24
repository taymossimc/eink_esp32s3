#pragma once

#include <ArduinoJson.h>

bool otaGetStatus(JsonObject out);
bool otaUpdateBegin(size_t expectedSize, String *errorOut = nullptr);
bool otaUpdateWriteChunk(const uint8_t *data, size_t length,
                         String *errorOut = nullptr);
bool otaUpdateFinish(String *errorOut = nullptr);
void otaUpdateAbort();
bool otaUpdateFromUrl(const char *url, String *errorOut = nullptr);
