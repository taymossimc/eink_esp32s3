#include "tmads_crypto.h"

#include "tmads_identity.h"

#include <SHA256.h>
#include <esp_random.h>
#include <sodium.h>
#include <time.h>

namespace tmads {

String randomNonce() {
    char buf[33] = {};
    for (size_t i = 0; i < 32; ++i) {
        snprintf(buf + i, 2, "%01x", static_cast<unsigned>(esp_random() & 0xF));
    }
    return String(buf);
}

String sha256HexLower(const uint8_t *data, size_t len) {
    SHA256 hash;
    hash.reset();
    hash.update(data, len);
    uint8_t digest[32] = {};
    hash.finalize(digest, sizeof(digest));
    char hex[65] = {};
    for (size_t i = 0; i < 32; ++i) {
        snprintf(hex + (i * 2), 3, "%02x", digest[i]);
    }
    return String(hex);
}

String base64UrlEncode(const uint8_t *data, size_t len) {
    static const char kEncode[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    String out;
    uint32_t val = 0;
    int valb = -8;
    for (size_t i = 0; i < len; ++i) {
        val = (val << 8) + data[i];
        valb += 8;
        while (valb >= 0) {
            out += kEncode[(val >> valb) & 0x3F];
            valb -= 6;
        }
    }
    if (valb > -8) out += kEncode[((val << (valb + 8)) & 0x3F)];
    return out;
}

SignedHeaders signRequest(const char *method, const char *path,
                          const uint8_t *body, size_t bodyLen) {
    SignedHeaders headers;
    headers.timestamp = String(static_cast<uint32_t>(time(nullptr)));
    headers.nonce = randomNonce();
    const String bodyHash = sha256HexLower(body, bodyLen);
    String methodUpper = String(method);
    methodUpper.toUpperCase();
    const String canonical = headers.timestamp + "\n" + headers.nonce + "\n" +
                             methodUpper + "\n" + String(path) + "\nsha256:" +
                             bodyHash;

    uint8_t signature[64] = {};
    uint8_t publicKey[crypto_sign_PUBLICKEYBYTES] = {};
    uint8_t secretKey[crypto_sign_SECRETKEYBYTES] = {};
    crypto_sign_seed_keypair(publicKey, secretKey, identityPrivateKey());
    unsigned long long signatureLength = 0;
    crypto_sign_detached(
        signature, &signatureLength,
        reinterpret_cast<const uint8_t *>(canonical.c_str()), canonical.length(),
        secretKey);
    sodium_memzero(secretKey, sizeof(secretKey));
    char encoded[sodium_base64_ENCODED_LEN(
        crypto_sign_BYTES, sodium_base64_VARIANT_URLSAFE_NO_PADDING)] = {};
    sodium_bin2base64(encoded, sizeof(encoded), signature, signatureLength,
                      sodium_base64_VARIANT_URLSAFE_NO_PADDING);
    headers.signature = "ed25519:" + String(encoded);
    return headers;
}

}  // namespace tmads
