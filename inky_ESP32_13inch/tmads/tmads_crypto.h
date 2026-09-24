#pragma once

#include <Arduino.h>

namespace tmads {

String randomNonce();
String sha256HexLower(const uint8_t *data, size_t len);
String base64UrlEncode(const uint8_t *data, size_t len);
struct SignedHeaders {
    String timestamp;
    String nonce;
    String signature;
};

SignedHeaders signRequest(const char *method, const char *path,
                          const uint8_t *body, size_t bodyLen);

}  // namespace tmads
