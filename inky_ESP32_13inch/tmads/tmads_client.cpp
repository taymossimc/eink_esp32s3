#include "tmads_client.h"

#include "battery/ups_battery.h"
#include "cache/frame_cache.h"
#include "config/firmware_version.h"
#include "config/device_config.h"
#include "display/display_panel.h"
#include "display/display_identity.h"
#include "image/image_pipeline.h"
#include "modem/modem.h"
#include "network/network_manager.h"
#include "ota/ota_update.h"
#include "storage/usb_campaign_storage.h"
#include "tmads_config.h"
#include "tmads_crypto.h"
#include "tmads_campaign_cache.h"
#include "tmads_identity.h"
#include "tmads_timing.h"

#include <ArduinoJson.h>
#include <Preferences.h>
#include <sodium.h>

#include <vector>

namespace tmads {
namespace {

bool gReady = false;

bool postJsonSigned(const char *signPath, const char *requestPath,
                    const String &body, uint8_t **responseOut,
                    size_t *responseLen, int *statusOut, String *errorOut) {
    const auto headers =
        signRequest("POST", signPath,
                    reinterpret_cast<const uint8_t *>(body.c_str()), body.length());
    bool ok = false;
    if (networkWifiConnected()) {
        ok = networkWifiHttpsRequest(
            TMADS_HOST, requestPath, "POST",
            reinterpret_cast<const uint8_t *>(body.c_str()), body.length(),
            headers.timestamp.c_str(), headers.nonce.c_str(),
            headers.signature.c_str(), responseOut, responseLen, 65536, statusOut);
    }
    if (!ok && modemIsDataConnected()) {
        ok = modemHttpsRequest(
            TMADS_HOST, requestPath, "POST",
            reinterpret_cast<const uint8_t *>(body.c_str()), body.length(),
            headers.timestamp.c_str(), headers.nonce.c_str(),
            headers.signature.c_str(), responseOut, responseLen, 65536, statusOut);
    }
    if (!ok && errorOut) *errorOut = "HTTPS request failed";
    return ok;
}

bool getSigned(const char *signPath, const char *requestPath, uint8_t **responseOut,
               size_t *responseLen, size_t maxBytes, String *errorOut) {
    const auto headers = signRequest("GET", signPath, nullptr, 0);
    bool ok = false;
    if (networkWifiConnected()) {
        ok = networkWifiHttpsRequest(
            TMADS_HOST, requestPath, "GET", nullptr, 0,
            headers.timestamp.c_str(), headers.nonce.c_str(),
            headers.signature.c_str(), responseOut, responseLen, maxBytes,
            nullptr);
    }
    if (!ok && modemIsDataConnected()) {
        ok = modemHttpsRequest(
            TMADS_HOST, requestPath, "GET", nullptr, 0,
            headers.timestamp.c_str(), headers.nonce.c_str(),
            headers.signature.c_str(), responseOut, responseLen, maxBytes,
            nullptr);
    }
    if (!ok && errorOut) *errorOut = "HTTPS download failed";
    return ok;
}

FirmwareOffer parseFirmwareOffer(JsonObjectConst root) {
    FirmwareOffer offer;
    JsonObjectConst fw = root["firmware_offer"];
    if (fw.isNull()) return offer;
    offer.version = fw["version"].as<String>();
    offer.artifactHash = fw["artifact_hash"].as<String>();
    offer.urlPath = fw["url"].as<String>();
    offer.byteLength = fw["byte_length"].as<size_t>();
    offer.available =
        offer.version.length() > 0 && offer.artifactHash.length() > 0 &&
        offer.urlPath.length() > 0 && offer.byteLength > 0;
    return offer;
}

template <size_t N>
void copyText(char (&destination)[N], const char *source) {
    if (!source) {
        destination[0] = '\0';
        return;
    }
    strncpy(destination, source, N - 1);
    destination[N - 1] = '\0';
}

bool verifySignedDocument(JsonDocument &doc, String *errorOut) {
    const char *signer = doc["signer_key_id"] | "";
    const char *signatureText = doc["signature"] | "";
    const char *payloadText = doc["_signed_payload"] | "";
    if (strcmp(signer, TMADS_EDGE_SIGNER_KEY_ID) != 0 ||
        strncmp(signatureText, "ed25519:", 8) != 0 ||
        payloadText[0] == '\0') {
        if (errorOut) *errorOut = "missing or untrusted edge signature";
        return false;
    }
    if (sodium_init() < 0) {
        if (errorOut) *errorOut = "signature verifier unavailable";
        return false;
    }

    uint8_t publicKey[crypto_sign_PUBLICKEYBYTES] = {};
    size_t publicKeyLength = 0;
    if (sodium_base642bin(
            publicKey, sizeof(publicKey), TMADS_EDGE_PUBLIC_KEY_B64,
            strlen(TMADS_EDGE_PUBLIC_KEY_B64), nullptr, &publicKeyLength,
            nullptr, sodium_base64_VARIANT_URLSAFE_NO_PADDING) != 0 ||
        publicKeyLength != sizeof(publicKey)) {
        if (errorOut) *errorOut = "invalid pinned edge public key";
        return false;
    }

    uint8_t signature[crypto_sign_BYTES] = {};
    size_t signatureLength = 0;
    const char *encodedSignature = signatureText + 8;
    if (sodium_base642bin(
            signature, sizeof(signature), encodedSignature,
            strlen(encodedSignature), nullptr, &signatureLength, nullptr,
            sodium_base64_VARIANT_URLSAFE_NO_PADDING) != 0 ||
        signatureLength != sizeof(signature)) {
        if (errorOut) *errorOut = "malformed edge signature";
        return false;
    }

    const size_t encodedLength = strlen(payloadText);
    const size_t payloadCapacity = encodedLength * 3U / 4U + 4U;
    uint8_t *payload = static_cast<uint8_t *>(
        heap_caps_malloc(payloadCapacity, MALLOC_CAP_SPIRAM));
    if (!payload) payload = static_cast<uint8_t *>(malloc(payloadCapacity));
    if (!payload) {
        if (errorOut) *errorOut = "signed payload allocation failed";
        return false;
    }
    size_t payloadLength = 0;
    const bool decoded =
        sodium_base642bin(
            payload, payloadCapacity, payloadText, encodedLength, nullptr,
            &payloadLength, nullptr,
            sodium_base64_VARIANT_URLSAFE_NO_PADDING) == 0;
    const bool verified =
        decoded &&
        crypto_sign_verify_detached(signature, payload, payloadLength,
                                    publicKey) == 0;
    if (!verified) {
        heap_caps_free(payload);
        if (errorOut) *errorOut = "edge response signature verification failed";
        return false;
    }

    doc.clear();
    const DeserializationError parseError =
        deserializeJson(doc, payload, payloadLength);
    heap_caps_free(payload);
    if (parseError) {
        if (errorOut) *errorOut = "verified edge payload is invalid JSON";
        return false;
    }
    return true;
}

const char *deviceVariant() {
    const auto orientation = device_config::getSnapshot().displayOrientation;
    return (orientation == device_config::DisplayOrientation::Landscape ||
            orientation == device_config::DisplayOrientation::LandscapeInverted)
               ? "landscape"
               : "portrait";
}

bool downloadCatalogAsset(const CatalogAsset &asset, String *errorOut) {
    char signPath[256] = {};
    char requestPath[288] = {};
    snprintf(signPath, sizeof(signPath),
             "/api/v1/edge/%s/media-rendered/%s", identityDeviceId(),
             asset.assetHash);
    snprintf(requestPath, sizeof(requestPath),
             "%s/api/v1/edge/%s/media-rendered/%s?variant=%s",
             TMADS_API_PREFIX, identityDeviceId(), asset.assetHash,
             asset.variant);
    uint8_t *body = nullptr;
    size_t bodyLength = 0;
    if (!getSigned(signPath, requestPath, &body, &bodyLength,
                   3UL * 1024UL * 1024UL, errorOut)) {
        return false;
    }
    const bool stored = campaignCacheStoreRendered(
        asset.renderedHash, asset.byteLength, body, bodyLength);
    timings().downloadBytes += static_cast<uint32_t>(bodyLength);
    heap_caps_free(body);
    if (!stored && errorOut) {
        *errorOut = "verification or cache write failed";
    }
    return stored;
}

bool displayCatalogAsset(const CatalogAsset &asset, String *errorOut) {
    const BatteryTelemetry battery = batteryGetCached();
    if (battery.available && battery.socPct >= 0 && battery.socPct < 15) {
        if (errorOut) {
            *errorOut = "display refresh deferred below 15% battery";
        }
        return false;
    }
    uint8_t *encoded = nullptr;
    size_t encodedLength = 0;
    if (!campaignCacheLoadRendered(asset.renderedHash, &encoded,
                                   &encodedLength)) {
        if (errorOut) *errorOut = "cached campaign asset unavailable";
        return false;
    }
    uint8_t *frame = static_cast<uint8_t *>(
        heap_caps_malloc(FRAME_CACHE_PAYLOAD_BYTES, MALLOC_CAP_SPIRAM));
    if (!frame) {
        heap_caps_free(encoded);
        if (errorOut) *errorOut = "frame allocation failed";
        return false;
    }
    size_t frameLength = 0;
    const uint16_t orientation = static_cast<uint16_t>(
        device_config::getSnapshot().displayOrientation);
    const uint32_t decodeStart = millis();
    const bool decoded =
        imageDecodeCropFill(encoded, encodedLength, "campaign.jpg", frame,
                            FRAME_CACHE_PAYLOAD_BYTES, &frameLength, orientation);
    timings().decodeMs += millis() - decodeStart;
    heap_caps_free(encoded);
    if (!decoded || frameLength != FRAME_CACHE_PAYLOAD_BYTES) {
        heap_caps_free(frame);
        if (errorOut) *errorOut = "campaign JPEG decode failed";
        return false;
    }

    const uint32_t displayStart = millis();
    bool shown = displayShowPackedImage(frame, FRAME_CACHE_PAYLOAD_BYTES);
    if (!shown) {
        Serial.println("TMAds: panel refresh failed; resetting and retrying once");
        displaySleepBeforePowerOff();
        delay(30000);
        shown = displayShowPackedImage(frame, FRAME_CACHE_PAYLOAD_BYTES);
    }
    timings().displayMs += millis() - displayStart;
    timings().spiTransferMs += displayLastTransferMs();
    timings().busyRefreshMs += displayLastBusyRefreshMs();
    if (shown) {
        frameCacheStoreSlot(FrameCacheSlot::Current, frame,
                            FRAME_CACHE_PAYLOAD_BYTES, asset.assetHash,
                            orientation, "tmads");
        Preferences prefs;
        if (prefs.begin("tmads_campaign", false)) {
            prefs.putString("active_hash", asset.assetHash);
            prefs.end();
        }
    }
    heap_caps_free(frame);
    if (!shown && errorOut) *errorOut = "display refresh failed";
    return shown;
}

void appendAssetError(CampaignSyncResult &result, const char *assetHash,
                      const String &error) {
    if (result.assetErrors.length() > 480) return;
    if (result.assetErrors.length()) result.assetErrors += "; ";
    result.assetErrors += String(assetHash).substring(0, 23);
    result.assetErrors += ": ";
    result.assetErrors += error;
}

}  // namespace

bool clientBegin() {
    if (gReady) return true;
    gReady = identityBegin();
    if (gReady) {
        Preferences prefs;
        if (prefs.begin("tmads_campaign", false)) {
            const String renderedFirmware =
                prefs.getString("render_fw", "");
            if (renderedFirmware != FIRMWARE_VERSION) {
                prefs.remove("active_hash");
                prefs.putString("render_fw", FIRMWARE_VERSION);
            }
            prefs.end();
        }
    }
    return gReady;
}

bool clientEnrollIfNeeded(String *errorOut) {
    if (errorOut) errorOut->clear();
    if (!clientBegin()) {
        if (errorOut) *errorOut = "identity init failed";
        return false;
    }
    if (identityIsEnrolled()) return true;

    Preferences prefs;
    if (!prefs.begin("tmads", true)) {
        if (errorOut) *errorOut = "prefs unavailable";
        return false;
    }
    const String token = prefs.getString("enroll_token", "");
    prefs.end();
    if (token.isEmpty()) {
        if (errorOut) *errorOut = "missing enrollment token";
        return false;
    }

    JsonDocument doc;
    doc["device_id"] = identityDeviceId();
    doc["enrollment_token"] = token;
    doc["public_key"] = identityPublicKeyWire();
    String body;
    serializeJson(doc, body);

    uint8_t *response = nullptr;
    size_t responseLen = 0;
    int status = 0;
    const bool ok = postJsonSigned(TMADS_ENROLL_SIGN_PATH,
                                   TMADS_ENROLL_REQUEST_PATH, body, &response,
                                   &responseLen, &status, errorOut);
    if (response) heap_caps_free(response);
    if (!ok) {
        if (errorOut && errorOut->isEmpty()) {
            *errorOut = String("enroll HTTP ") + status;
        }
        return false;
    }

    if (!prefs.begin("tmads", false)) {
        if (errorOut) *errorOut = "prefs write failed";
        return false;
    }
    prefs.putBool("enrolled", true);
    prefs.remove("enroll_token");
    prefs.end();
    Serial.printf("TMAds: enrolled device %s\n", identityDeviceId());
    return true;
}

CheckInResult clientCheckIn(const char *wakeReason) {
    CheckInResult result;
    if (!clientBegin()) {
        result.error = "identity init failed";
        return result;
    }
    if (!clientEnrollIfNeeded(&result.error)) return result;

    JsonDocument doc;
    doc["software_version"] = FIRMWARE_VERSION;
    doc["boot_id"] = identityBootId();
    doc["uptime_seconds"] = millis() / 1000U;
    doc["wake_reason"] = wakeReason ? wakeReason : "unknown";
    JsonObject connectivity = doc["connectivity"].to<JsonObject>();
    connectivity["transport"] =
        networkWifiConnected() ? "wifi"
                               : (modemIsDataConnected() ? "cellular" : "none");
    JsonObject wifi = connectivity["wifi"].to<JsonObject>();
    wifi["connected"] = networkWifiConnected();
    if (networkWifiConnected()) {
        wifi["ssid"] = networkWifiSsid();
        wifi["ip"] = networkWifiIp();
        wifi["rssi_dbm"] = networkWifiRssi();
    }
    const ModemDiagnostics modem = modemGetDiagnostics();
    JsonObject cellular = connectivity["cellular"].to<JsonObject>();
    cellular["ready"] = modem.ready;
    cellular["registered"] = modem.networkRegistered;
    cellular["data_connected"] = modem.dataConnected;
    cellular["signal_quality"] = modem.signalQuality;
    cellular["operator"] = modem.operatorName;
    cellular["ip"] = modem.cellularIp;
    cellular["imei"] = modem.imei;
    cellular["iccid"] = modem.iccid;

    JsonObject gnss = doc["gnss"].to<JsonObject>();
    gnss["fixed"] = modem.gnssFixed;
    if (modem.gnssFixed) {
        gnss["latitude"] = modem.latitude;
        gnss["longitude"] = modem.longitude;
        gnss["altitude_m"] = modem.altitude;
        gnss["satellites"] = modem.satellites;
        gnss["observed_at"] = modem.gnssUtc;
    }

    BatteryTelemetry battery;
    const bool batteryAvailable = batteryRead(&battery);
    JsonObject batteryJson = doc["battery"].to<JsonObject>();
    batteryJson["available"] = batteryAvailable;
    if (batteryAvailable) {
        batteryJson["voltage_v"] = battery.busVoltageV;
        batteryJson["current_ma"] = battery.currentMa;
        batteryJson["power_mw"] = battery.powerMw;
        batteryJson["soc_percent"] = battery.socPct;
    } else {
        batteryJson["error"] = battery.error;
    }
    JsonObject display = doc["display"].to<JsonObject>();
    display["initialized"] = displayPanelInitialized();
    display["last_error"] = displayLastError();
    display["orientation"] = static_cast<uint16_t>(
        device_config::getSnapshot().displayOrientation);
    const DisplayIdentity displayIdentity = displayIdentityGet();
    display["eeprom"]["present"] = displayIdentity.present;
    display["eeprom"]["width"] = displayIdentity.width;
    display["eeprom"]["height"] = displayIdentity.height;
    display["eeprom"]["color"] = displayIdentity.color;
    display["eeprom"]["pcb_variant"] = displayIdentity.pcbVariant;
    display["eeprom"]["display_variant"] = displayIdentity.displayVariant;
    Preferences displayPrefs;
    if (displayPrefs.begin("tmads_campaign", true)) {
        display["active_asset_hash"] =
            displayPrefs.getString("active_hash", "");
        display["last_campaign_error"] =
            displayPrefs.getString("last_error", "");
        displayPrefs.end();
    }
    const UsbCampaignStorageStatus usbStorage =
        usbCampaignStorageGetStatus();
    JsonObject storage = doc["storage"]["usb"].to<JsonObject>();
    storage["host_ready"] = usbStorage.hostReady;
    storage["connected"] = usbStorage.connected;
    storage["mounted"] = usbStorage.mounted;
    storage["writable"] = usbStorage.writable;
    storage["capacity_bytes"] = usbStorage.capacityBytes;
    storage["free_bytes"] = usbStorage.freeBytes;
    storage["product"] = usbStorage.product;
    storage["error"] = usbStorage.error;
    CampaignCacheStatus cacheStatus;
    if (campaignCacheGetStatus(&cacheStatus)) {
        JsonObject cache = doc["storage"]["campaign_cache"].to<JsonObject>();
        cache["assets"] = cacheStatus.assetCount;
        cache["cached"] = cacheStatus.cachedCount;
        cache["usb_campaign_bytes"] = cacheStatus.usbCampaignBytes;
        cache["littlefs_used_bytes"] = cacheStatus.usedBytes;
    }
    const SyncTimings &lastTimings = timings();
    if (lastTimings.completedAtMs != 0) {
        JsonObject timing = doc["timings"].to<JsonObject>();
        timing["boot_to_sync_ms"] = lastTimings.bootToSyncMs;
        timing["connect_ms"] = lastTimings.connectMs;
        timing["checkin_ms"] = lastTimings.checkInMs;
        timing["manifest_fetch_ms"] = lastTimings.manifestFetchMs;
        timing["catalog_fetch_ms"] = lastTimings.catalogFetchMs;
        timing["cache_reconcile_ms"] = lastTimings.cacheReconcileMs;
        timing["download_ms"] = lastTimings.downloadMs;
        timing["download_bytes"] = lastTimings.downloadBytes;
        timing["download_count"] = lastTimings.downloadCount;
        timing["decode_ms"] = lastTimings.decodeMs;
        timing["display_ms"] = lastTimings.displayMs;
        timing["spi_transfer_ms"] = lastTimings.spiTransferMs;
        timing["busy_refresh_ms"] = lastTimings.busyRefreshMs;
        timing["ack_ms"] = lastTimings.ackMs;
        timing["pre_sleep_report_ms"] = lastTimings.preSleepReportMs;
        timing["total_ms"] = lastTimings.totalMs;
        timing["transport"] = lastTimings.transport;
        timing["display_changed"] = lastTimings.displayChanged;
    }
    String body;
    serializeJson(doc, body);

    char signPath[128] = {};
    char requestPath[160] = {};
    snprintf(signPath, sizeof(signPath), "/api/v1/edge/%s/check-in",
             identityDeviceId());
    snprintf(requestPath, sizeof(requestPath), "%s/api/v1/edge/%s/check-in",
             TMADS_API_PREFIX, identityDeviceId());

    uint8_t *response = nullptr;
    size_t responseLen = 0;
    int status = 0;
    const uint32_t checkInStart = millis();
    if (!postJsonSigned(signPath, requestPath, body, &response, &responseLen,
                        &status, &result.error)) {
        if (result.error.isEmpty()) result.error = String("check-in HTTP ") + status;
        return result;
    }
    timings().checkInMs = millis() - checkInStart;

    JsonDocument parsed;
    if (deserializeJson(parsed, response, responseLen)) {
        heap_caps_free(response);
        result.error = "invalid check-in JSON";
        return result;
    }
    heap_caps_free(response);
    if (!verifySignedDocument(parsed, &result.error)) {
        return result;
    }

    result.firmware = parseFirmwareOffer(parsed.as<JsonObjectConst>());
    result.wakeIntervalSec = parsed["wake_interval_seconds"] | 0U;
    if (result.wakeIntervalSec > 0) {
        identitySetWakeIntervalSec(result.wakeIntervalSec);
    }
    result.awakeTimeoutSec = parsed["awake_timeout_seconds"] | 0U;
    if (result.awakeTimeoutSec > 0) {
        identitySetAwakeTimeoutSec(result.awakeTimeoutSec);
    }
    result.awakePollSec = parsed["awake_poll_seconds"] | 0U;
    if (result.awakePollSec > 0) {
        identitySetAwakePollSec(result.awakePollSec);
    }
    identitySetNextWakeAt(parsed["server_time"] | "",
                          parsed["next_wake_at"] | "");
    result.displayOrientation = parsed["display_orientation"] | -1;
    result.ok = true;
    return result;
}

bool clientDownloadFirmware(const FirmwareOffer &offer, String *errorOut) {
    if (errorOut) errorOut->clear();
    if (!offer.available) {
        if (errorOut) *errorOut = "no firmware offer";
        return false;
    }

    char signPath[192] = {};
    char requestPath[224] = {};
    snprintf(signPath, sizeof(signPath), "/api/v1/edge/%s/firmware/%s",
             identityDeviceId(), offer.artifactHash.c_str());
    snprintf(requestPath, sizeof(requestPath), "%s/api/v1/edge/%s/firmware/%s",
             TMADS_API_PREFIX, identityDeviceId(), offer.artifactHash.c_str());

    uint8_t *firmware = nullptr;
    size_t firmwareLen = 0;
    if (!getSigned(signPath, requestPath, &firmware, &firmwareLen,
                   TMADS_MAX_FIRMWARE_BYTES, errorOut)) {
        return false;
    }
    if (offer.artifactHash.startsWith("sha256:")) {
        char hex[65] = {};
        campaignSha256Hex(firmware, firmwareLen, hex);
        if (offer.artifactHash.substring(7) != hex) {
            heap_caps_free(firmware);
            if (errorOut) *errorOut = "firmware hash mismatch";
            return false;
        }
    }

    if (!otaUpdateBegin(firmwareLen, errorOut)) {
        heap_caps_free(firmware);
        return false;
    }
    if (!otaUpdateWriteChunk(firmware, firmwareLen, errorOut)) {
        heap_caps_free(firmware);
        otaUpdateAbort();
        return false;
    }
    heap_caps_free(firmware);
    identityMarkOtaRebootPending();
    if (!otaUpdateFinish(errorOut)) return false;
    return true;
}

CampaignSyncResult clientSyncCampaigns() {
    CampaignSyncResult result;
    if (!clientBegin()) {
        result.error = "identity init failed";
        return result;
    }
    if (!campaignCacheBegin()) {
        result.error = "campaign cache unavailable";
        return result;
    }
    const uint32_t syncStart = millis();
    copyText(timings().transport,
             networkWifiConnected() ? "wifi" : "cellular");

    // 1. Targeted manifest: the authoritative display list.
    char signPath[256] = {};
    char requestPath[288] = {};
    snprintf(signPath, sizeof(signPath), "/api/v1/edge/%s/manifest",
             identityDeviceId());
    snprintf(requestPath, sizeof(requestPath), "%s/api/v1/edge/%s/manifest",
             TMADS_API_PREFIX, identityDeviceId());

    const uint32_t manifestStart = millis();
    uint8_t *manifestBody = nullptr;
    size_t manifestLength = 0;
    if (!getSigned(signPath, requestPath, &manifestBody, &manifestLength,
                   512UL * 1024UL, &result.error)) {
        return result;
    }
    JsonDocument manifest;
    const DeserializationError parseError =
        deserializeJson(manifest, manifestBody, manifestLength);
    heap_caps_free(manifestBody);
    if (parseError) {
        result.error = "invalid campaign manifest JSON";
        return result;
    }
    if (!verifySignedDocument(manifest, &result.error)) {
        return result;
    }
    timings().manifestFetchMs = millis() - manifestStart;

    std::vector<String> eligibleHashes;   // Original asset hashes, in order.
    std::vector<String> contractHashes;
    for (JsonObjectConst contractItem :
         manifest["contracts"].as<JsonArrayConst>()) {
        contractHashes.push_back(contractItem["object_hash"].as<String>());
        JsonObjectConst payload = contractItem["payload"];
        for (JsonObjectConst media : payload["media"].as<JsonArrayConst>()) {
            if (strcmp(media["profile"] | "", "tmads-portrait-v1") != 0) {
                continue;
            }
            const char *assetHash = media["asset_hash"] | "";
            if (!assetHash[0]) continue;
            bool duplicate = false;
            for (const String &existing : eligibleHashes) {
                if (existing == assetHash) {
                    duplicate = true;
                    break;
                }
            }
            if (!duplicate) eligibleHashes.emplace_back(assetHash);
        }
    }
    const String cursor = manifest["cursor"].as<String>();
    manifest.clear();

    // 2. Preload catalog: every campaign asset with rendered hashes.
    const char *variant = deviceVariant();
    snprintf(signPath, sizeof(signPath), "/api/v1/edge/%s/catalog",
             identityDeviceId());
    snprintf(requestPath, sizeof(requestPath), "%s/api/v1/edge/%s/catalog",
             TMADS_API_PREFIX, identityDeviceId());
    const uint32_t catalogStart = millis();
    uint8_t *catalogBody = nullptr;
    size_t catalogLength = 0;
    std::vector<CatalogAsset> assets;
    if (getSigned(signPath, requestPath, &catalogBody, &catalogLength,
                  512UL * 1024UL, nullptr)) {
        JsonDocument catalog;
        const DeserializationError catalogError =
            deserializeJson(catalog, catalogBody, catalogLength);
        heap_caps_free(catalogBody);
        if (catalogError) {
            result.error = "invalid campaign catalog JSON";
            return result;
        }
        String catalogSignatureError;
        if (!verifySignedDocument(catalog, &catalogSignatureError)) {
            result.error = catalogSignatureError;
            return result;
        }
        {
            for (JsonObjectConst campaign :
                 catalog["campaigns"].as<JsonArrayConst>()) {
                const char *campaignId = campaign["campaign_id"] | "";
                const char *status = campaign["status"] | "";
                for (JsonObjectConst mediaItem :
                     campaign["assets"].as<JsonArrayConst>()) {
                    // Retain both device derivatives. Switching between
                    // portrait and landscape then reuses USB content instead
                    // of downloading the whole catalog again.
                    for (const char *catalogVariant :
                         {"portrait", "landscape"}) {
                        JsonObjectConst variantMeta =
                            mediaItem["variants"][catalogVariant];
                        if (variantMeta.isNull()) continue;
                        CatalogAsset asset;
                        copyText(asset.assetHash,
                                 mediaItem["asset_hash"] | "");
                        copyText(asset.renderedHash,
                                 variantMeta["rendered_hash"] | "");
                        copyText(asset.campaignId, campaignId);
                        copyText(asset.mediaId,
                                 mediaItem["media_id"] | "");
                        copyText(asset.campaignStatus, status);
                        copyText(asset.variant, catalogVariant);
                        asset.byteLength = variantMeta["bytes"] | 0U;
                        bool duplicate = false;
                        for (const CatalogAsset &existing : assets) {
                            if (strcmp(existing.renderedHash,
                                       asset.renderedHash) == 0) {
                                duplicate = true;
                                break;
                            }
                        }
                        if (!duplicate) assets.push_back(asset);
                    }
                }
            }
        }
    } else {
        Serial.println("TMAds: preload catalog unavailable; using cached copy");
        for (size_t i = 0; i < campaignCatalogCount(); ++i) {
            CatalogAsset asset;
            if (campaignCatalogGet(i, &asset)) {
                assets.push_back(asset);
            }
        }
    }
    timings().catalogFetchMs = millis() - catalogStart;

    // Mark display eligibility from the targeted manifest.
    for (CatalogAsset &asset : assets) {
        asset.eligible = false;
        for (const String &eligible : eligibleHashes) {
            if (eligible == asset.assetHash) {
                asset.eligible = true;
                break;
            }
        }
    }

    // 3. Download missing assets — eligible first, partial progress allowed.
    const uint32_t downloadStart = millis();
    for (int pass = 0; pass < 2; ++pass) {
        for (CatalogAsset &asset : assets) {
            const bool wantedThisPass = (pass == 0) == asset.eligible;
            if (!wantedThisPass) continue;
            if (campaignCacheHasRendered(asset.renderedHash)) {
                asset.cached = true;
                continue;
            }
            Serial.printf("TMAds: downloading %s campaign %s asset %s (%s)\n",
                          asset.eligible ? "eligible" : "preload",
                          asset.campaignId, asset.assetHash, asset.variant);
            String assetError;
            if (downloadCatalogAsset(asset, &assetError)) {
                asset.cached = true;
                result.downloadedCount++;
                timings().downloadCount++;
            } else {
                appendAssetError(result, asset.assetHash, assetError);
                Serial.printf("TMAds: asset %s failed: %s\n", asset.assetHash,
                              assetError.c_str());
            }
        }
    }
    timings().downloadMs = millis() - downloadStart;

    const uint32_t reconcileStart = millis();
    if (!campaignCacheCommitCatalog(cursor.c_str(), assets)) {
        Serial.println("TMAds: catalog persistence failed (continuing)");
    }
    timings().cacheReconcileMs = millis() - reconcileStart;
    result.assetCount = assets.size();
    for (const CatalogAsset &asset : assets) {
        if (asset.cached) result.cachedCount++;
    }

    // 4. Selection/rotation across eligible cached assets.
    std::vector<const CatalogAsset *> displayable;
    for (const CatalogAsset &asset : assets) {
        if (asset.eligible && asset.cached &&
            strcmp(asset.campaignStatus, "ACTIVE") == 0 &&
            strcmp(asset.variant, variant) == 0) {
            displayable.push_back(&asset);
        }
    }
    if (!displayable.empty()) {
        // Rotate to the next eligible asset once per wake cycle; awake polls
        // only refresh when the active asset is no longer eligible.
        static bool gRotatedThisBoot = false;
        Preferences prefs;
        String activeHash;
        if (prefs.begin("tmads_campaign", true)) {
            activeHash = prefs.getString("active_hash", "");
            prefs.end();
        }
        int activeIndex = -1;
        for (size_t i = 0; i < displayable.size(); ++i) {
            if (activeHash == displayable[i]->assetHash) {
                activeIndex = static_cast<int>(i);
                break;
            }
        }
        const CatalogAsset *selected = nullptr;
        if (activeIndex < 0) {
            selected = displayable[0];
        } else if (!gRotatedThisBoot && displayable.size() > 1) {
            selected = displayable[(activeIndex + 1) % displayable.size()];
        }
        if (selected) {
            gRotatedThisBoot = true;
            Serial.printf("TMAds: displaying campaign %s (%u eligible)\n",
                          selected->campaignId,
                          static_cast<unsigned>(displayable.size()));
            String displayError;
            if (displayCatalogAsset(*selected, &displayError)) {
                result.displayChanged = true;
            } else {
                appendAssetError(result, selected->assetHash, displayError);
            }
        }
    }

    // 5. Acknowledge the sync, reporting partial failures.
    JsonDocument ack;
    ack["cursor"] = cursor;
    JsonArray applied = ack["applied_contract_hashes"].to<JsonArray>();
    for (const String &hash : contractHashes) applied.add(hash);
    JsonArray cached = ack["cached_media_hashes"].to<JsonArray>();
    for (const CatalogAsset &asset : assets) {
        if (!asset.cached || !asset.eligible) continue;
        bool alreadyReported = false;
        for (JsonVariantConst reported : cached) {
            if (strcmp(reported.as<const char *>(), asset.assetHash) == 0) {
                alreadyReported = true;
                break;
            }
        }
        if (!alreadyReported) cached.add(asset.assetHash);
    }
    JsonArray errors = ack["errors"].to<JsonArray>();
    if (result.assetErrors.length()) {
        JsonObject error = errors.add<JsonObject>();
        error["code"] = "partial_asset_failure";
        error["detail"] = result.assetErrors;
    }
    String ackBody;
    serializeJson(ack, ackBody);

    snprintf(signPath, sizeof(signPath), "/api/v1/edge/%s/sync/ack",
             identityDeviceId());
    snprintf(requestPath, sizeof(requestPath), "%s/api/v1/edge/%s/sync/ack",
             TMADS_API_PREFIX, identityDeviceId());
    const uint32_t ackStart = millis();
    uint8_t *ackResponse = nullptr;
    size_t ackResponseLength = 0;
    int ackStatus = 0;
    if (!postJsonSigned(signPath, requestPath, ackBody, &ackResponse,
                        &ackResponseLength, &ackStatus, &result.error)) {
        if (ackResponse) heap_caps_free(ackResponse);
        return result;
    }
    if (ackResponse) heap_caps_free(ackResponse);
    timings().ackMs = millis() - ackStart;
    timings().totalMs = millis() - syncStart;
    timings().displayChanged = result.displayChanged;
    timings().completedAtMs = millis();
    Serial.printf(
        "TMAds timings: manifest=%ums catalog=%ums reconcile=%ums "
        "download=%ums (%u assets, %u bytes) decode=%ums display=%ums "
        "(spi=%ums busy=%ums) ack=%ums total=%ums\n",
        timings().manifestFetchMs, timings().catalogFetchMs,
        timings().cacheReconcileMs, timings().downloadMs,
        timings().downloadCount,
        timings().downloadBytes, timings().decodeMs, timings().displayMs,
        timings().spiTransferMs, timings().busyRefreshMs,
        timings().ackMs, timings().totalMs);
    result.ok = true;
    return result;
}

}  // namespace tmads
