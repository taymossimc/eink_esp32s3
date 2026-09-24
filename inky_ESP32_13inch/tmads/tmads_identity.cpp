#include "tmads_identity.h"

#include "tmads_config.h"

#include <Preferences.h>
#include <esp_mac.h>
#include <esp_random.h>
#include <esp_sleep.h>
#include <sodium.h>
#include <time.h>

namespace tmads {
namespace {

Preferences gPrefs;
char gDeviceId[48] = {};
uint8_t gPrivateKey[32] = {};
uint8_t gPublicKey[32] = {};
char gBootId[17] = {};
bool gReady = false;
bool gLowBatteryOverride = false;

void ensureBootId() {
    if (gBootId[0] != '\0') return;
    snprintf(gBootId, sizeof(gBootId), "%08x%08x",
             static_cast<unsigned>(esp_random()),
             static_cast<unsigned>(esp_random()));
}

void buildDeviceIdFromMac() {
    uint8_t mac[6] = {};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(gDeviceId, sizeof(gDeviceId),
             "tmads-eink-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2],
             mac[3], mac[4], mac[5]);
}

void ensureKeypair() {
    const size_t privLen = gPrefs.getBytesLength("priv");
    if (sodium_init() < 0) return;
    if (privLen == 32 &&
        gPrefs.getBytes("priv", gPrivateKey, sizeof(gPrivateKey)) == 32) {
        uint8_t secretKey[crypto_sign_SECRETKEYBYTES] = {};
        crypto_sign_seed_keypair(gPublicKey, secretKey, gPrivateKey);
        sodium_memzero(secretKey, sizeof(secretKey));
        gPrefs.putBytes("pub", gPublicKey, sizeof(gPublicKey));
        return;
    }
    randombytes_buf(gPrivateKey, sizeof(gPrivateKey));
    uint8_t secretKey[crypto_sign_SECRETKEYBYTES] = {};
    crypto_sign_seed_keypair(gPublicKey, secretKey, gPrivateKey);
    sodium_memzero(secretKey, sizeof(secretKey));
    gPrefs.putBytes("priv", gPrivateKey, sizeof(gPrivateKey));
    gPrefs.putBytes("pub", gPublicKey, sizeof(gPublicKey));
}

}  // namespace

bool identityBegin() {
    if (gReady) return true;
    if (!gPrefs.begin("tmads", false)) return false;

    gPrefs.getString("device_id", gDeviceId, sizeof(gDeviceId));
    if (gDeviceId[0] == '\0') {
        buildDeviceIdFromMac();
        gPrefs.putString("device_id", gDeviceId);
    }

    ensureKeypair();
    ensureBootId();
    gReady = true;
    Serial.printf("TMAds identity: %s public=%s enrolled=%s\n", gDeviceId,
                  identityPublicKeyWire().c_str(),
                  gPrefs.getBool("enrolled", false) ? "yes" : "no");
    return true;
}

bool identityIsEnrolled() { return gReady && gPrefs.getBool("enrolled", false); }

const char *identityDeviceId() { return gDeviceId; }

const uint8_t *identityPrivateKey() { return gPrivateKey; }

const uint8_t *identityPublicKey() { return gPublicKey; }

String identityPublicKeyWire() {
    char encoded[sodium_base64_ENCODED_LEN(
        crypto_sign_PUBLICKEYBYTES,
        sodium_base64_VARIANT_URLSAFE_NO_PADDING)] = {};
    sodium_bin2base64(encoded, sizeof(encoded), gPublicKey, sizeof(gPublicKey),
                      sodium_base64_VARIANT_URLSAFE_NO_PADDING);
    return "ed25519:" + String(encoded);
}

String identityBootId() { return String(gBootId); }

void identitySetEnrollmentToken(const char *token) {
    if (!token) return;
    identityBegin();
    gPrefs.putString("enroll_token", token);
    gPrefs.putBool("enrolled", false);
}

uint32_t identityWakeIntervalSec() {
    const uint32_t stored = gPrefs.getUInt("wake_sec", TMADS_DEFAULT_WAKE_INTERVAL_SEC);
    return stored > 0 ? stored : TMADS_DEFAULT_WAKE_INTERVAL_SEC;
}

void identitySetWakeIntervalSec(uint32_t seconds) {
    identityBegin();
    if (seconds < 300) seconds = 300;
    gPrefs.putUInt("wake_sec", seconds);
}

uint32_t identityAwakeTimeoutSec() {
    const uint32_t stored = gPrefs.getUInt("awake_sec", 300);
    return stored >= 60 && stored <= 3600 ? stored : 300;
}

void identitySetAwakeTimeoutSec(uint32_t seconds) {
    identityBegin();
    if (seconds < 60) seconds = 60;
    if (seconds > 3600) seconds = 3600;
    gPrefs.putUInt("awake_sec", seconds);
}

uint32_t identityAwakePollSec() {
    const uint32_t stored = gPrefs.getUInt("poll_sec", 30);
    return stored >= 10 && stored <= 3600 ? stored : 30;
}

void identitySetAwakePollSec(uint32_t seconds) {
    identityBegin();
    if (seconds < 10) seconds = 10;
    if (seconds > 3600) seconds = 3600;
    gPrefs.putUInt("poll_sec", seconds);
}

void identitySetNextWakeAt(const char *serverTime, const char *nextWakeAt) {
    if (!serverTime || !nextWakeAt) return;
    // The server emits RFC 3339 with optional fractional seconds and either
    // Z or +00:00. Parse the fixed UTC calendar prefix explicitly so trailing
    // precision/timezone syntax cannot make the wake silently fall back.
    if (strlen(serverTime) < 19 || strlen(nextWakeAt) < 19) return;
    char serverPrefix[20] = {};
    char wakePrefix[20] = {};
    memcpy(serverPrefix, serverTime, 19);
    memcpy(wakePrefix, nextWakeAt, 19);
    struct tm server = {};
    struct tm wake = {};
    if (!strptime(serverPrefix, "%Y-%m-%dT%H:%M:%S", &server) ||
        !strptime(wakePrefix, "%Y-%m-%dT%H:%M:%S", &wake)) {
        return;
    }
    // ESP32 Arduino does not expose timegm(); force mktime() to UTC.
    setenv("TZ", "UTC0", 1);
    tzset();
    const time_t serverEpoch = mktime(&server);
    const time_t wakeEpoch = mktime(&wake);
    if (serverEpoch <= 0 || wakeEpoch <= serverEpoch) return;
    uint32_t seconds = static_cast<uint32_t>(wakeEpoch - serverEpoch);
    if (seconds < 300) seconds = 300;
    if (seconds > 604800) seconds = 604800;
    gPrefs.putUInt("next_wake_sec", seconds);
}

void identitySetLowBatteryOverride(bool enabled) {
    gLowBatteryOverride = enabled;
}

bool identityScheduleTimerWake() {
    uint32_t seconds = gPrefs.getUInt("next_wake_sec", identityWakeIntervalSec());
    gPrefs.remove("next_wake_sec");
    if (gLowBatteryOverride) {
        // Conserve energy without losing fleet contact entirely.
        seconds = max(seconds, min(identityWakeIntervalSec() * 4U, 604800U));
    }
    Serial.printf("TMAds: scheduling timer wake in %u s%s\n", seconds,
                  gLowBatteryOverride ? " (low battery)" : "");
    esp_sleep_enable_timer_wakeup(static_cast<uint64_t>(seconds) * 1000000ULL);
    return true;
}

bool identityConsumedOtaRebootFlag() {
    identityBegin();
    if (!gPrefs.getBool("ota_reboot", false)) return false;
    gPrefs.putBool("ota_reboot", false);
    return true;
}

void identityMarkOtaRebootPending() {
    identityBegin();
    gPrefs.putBool("ota_reboot", true);
}

}  // namespace tmads
