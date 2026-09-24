#include "ota_update.h"

#include "config/firmware_version.h"
#include "network/network_manager.h"

#include <esp_http_client.h>
#include <esp_https_ota.h>
#include <esp_ota_ops.h>
#include <esp_system.h>

namespace {

esp_ota_handle_t gUploadHandle = 0;
const esp_partition_t *gUploadPartition = nullptr;
bool gUploadActive = false;

void otaResetUploadState() {
    gUploadHandle = 0;
    gUploadPartition = nullptr;
    gUploadActive = false;
}

bool isHttpsUrl(const char *url) {
    return url && strncmp(url, "https://", 8) == 0;
}

esp_err_t otaFromHttpUrl(const char *url) {
    esp_http_client_config_t httpConfig = {};
    httpConfig.url = url;
    httpConfig.timeout_ms = 120000;
    httpConfig.keep_alive_enable = true;
    httpConfig.buffer_size = 4096;
    httpConfig.buffer_size_tx = 1024;

    return esp_https_ota(&httpConfig);
}

}  // namespace

bool otaGetStatus(JsonObject out) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *next = esp_ota_get_next_update_partition(running);
    out["version"] = FIRMWARE_VERSION;
    out["build"] = FIRMWARE_BUILD;
    out["runningPartition"] = running ? running->label : "unknown";
    out["updatePartition"] =
        next ? next->label : (running ? running->label : "unknown");
    out["maxSketchSpace"] = ESP.getFreeSketchSpace();
    out["flashSize"] = ESP.getFlashChipSize();
    out["canUpdate"] = next != nullptr;
    return true;
}

bool otaUpdateBegin(size_t expectedSize, String *errorOut) {
    if (errorOut) errorOut->clear();
    otaUpdateAbort();

    gUploadPartition = esp_ota_get_next_update_partition(nullptr);
    if (!gUploadPartition) {
        if (errorOut) *errorOut = "no OTA partition available";
        return false;
    }

    const esp_err_t err = esp_ota_begin(
        gUploadPartition,
        expectedSize > 0 ? expectedSize : OTA_SIZE_UNKNOWN, &gUploadHandle);
    if (err != ESP_OK) {
        otaResetUploadState();
        if (errorOut) {
            *errorOut = String("ota begin failed: ") + esp_err_to_name(err);
        }
        return false;
    }

    gUploadActive = true;
    Serial.printf("OTA upload: writing to %s\n", gUploadPartition->label);
    return true;
}

bool otaUpdateWriteChunk(const uint8_t *data, size_t length, String *errorOut) {
    if (errorOut) errorOut->clear();
    if (!gUploadActive || !data || length == 0) {
        if (errorOut) *errorOut = "upload not active";
        return false;
    }

    const esp_err_t err = esp_ota_write(gUploadHandle, data, length);
    if (err != ESP_OK) {
        otaUpdateAbort();
        if (errorOut) {
            *errorOut = String("ota write failed: ") + esp_err_to_name(err);
        }
        return false;
    }
    return true;
}

void otaUpdateAbort() {
    if (gUploadActive) esp_ota_abort(gUploadHandle);
    otaResetUploadState();
}

bool otaUpdateFinish(String *errorOut) {
    if (errorOut) errorOut->clear();
    if (!gUploadActive || !gUploadPartition) {
        if (errorOut) *errorOut = "upload not active";
        return false;
    }

    esp_err_t err = esp_ota_end(gUploadHandle);
    gUploadHandle = 0;
    gUploadActive = false;
    if (err != ESP_OK) {
        otaResetUploadState();
        if (errorOut) {
            *errorOut = String("ota end failed: ") + esp_err_to_name(err);
        }
        return false;
    }

    err = esp_ota_set_boot_partition(gUploadPartition);
    if (err != ESP_OK) {
        otaResetUploadState();
        if (errorOut) {
            *errorOut = String("set boot partition failed: ") +
                        esp_err_to_name(err);
        }
        return false;
    }

    Serial.printf("OTA upload: staged on %s, rebooting\n",
                  gUploadPartition->label);
    otaResetUploadState();
    delay(250);
    esp_restart();
    return true;
}

bool otaUpdateFromUrl(const char *url, String *errorOut) {
    if (errorOut) errorOut->clear();
    if (!url || url[0] == '\0') {
        if (errorOut) *errorOut = "missing url";
        return false;
    }
    if (strncmp(url, "http://", 7) != 0 && !isHttpsUrl(url)) {
        if (errorOut) *errorOut = "url must start with http:// or https://";
        return false;
    }
    if (!networkWifiConnected() && !networkConnectSaved(20000)) {
        if (errorOut) *errorOut = "Wi-Fi unavailable for OTA download";
        return false;
    }

    Serial.printf("OTA: downloading from %s\n", url);
    const esp_err_t result = otaFromHttpUrl(url);
    if (result != ESP_OK) {
        if (errorOut) {
            *errorOut = String("OTA failed: ") + esp_err_to_name(result) +
                        " (" + String(static_cast<int>(result)) + ")";
        }
        Serial.printf("OTA: failed %s (%d)\n", esp_err_to_name(result), result);
        return false;
    }

    Serial.println("OTA: complete, rebooting");
    delay(250);
    esp_restart();
    return true;
}
