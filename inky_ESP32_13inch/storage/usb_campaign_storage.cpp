#include "usb_campaign_storage.h"

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <ff.h>
#include <sys/stat.h>
#include <usb/msc_host.h>
#include <usb/msc_host_vfs.h>
#include <usb/usb_host.h>
#include <wchar.h>

namespace {

constexpr char kMountPath[] = "/usb";
constexpr char kStorageDirectory[] = "/usb/tmads";
constexpr char kProbePath[] = "/usb/tmads/mount-test.txt";

QueueHandle_t gEvents = nullptr;
SemaphoreHandle_t gStatusMutex = nullptr;
UsbCampaignStorageStatus gStatus;
msc_host_device_handle_t gDevice = nullptr;
msc_host_vfs_handle_t gVfs = nullptr;
bool gStarted = false;

void setError(const char *message) {
    if (!gStatusMutex) return;
    xSemaphoreTake(gStatusMutex, portMAX_DELAY);
    strncpy(gStatus.error, message ? message : "", sizeof(gStatus.error) - 1);
    gStatus.error[sizeof(gStatus.error) - 1] = '\0';
    xSemaphoreGive(gStatusMutex);
}

void copyWide(char *destination, size_t capacity, const wchar_t *source) {
    if (!destination || !capacity) return;
    destination[0] = '\0';
    if (!source) return;
    const size_t converted = wcstombs(destination, source, capacity - 1);
    if (converted == static_cast<size_t>(-1)) {
        destination[0] = '\0';
    } else {
        destination[min(converted, capacity - 1)] = '\0';
    }
}

bool verifyWritable() {
    mkdir(kStorageDirectory, 0775);
    FILE *file = fopen(kProbePath, "w");
    if (!file) return false;
    const char payload[] = "tmads-usb-storage-ok\n";
    const bool written =
        fwrite(payload, 1, sizeof(payload) - 1, file) == sizeof(payload) - 1;
    fflush(file);
    fclose(file);
    if (!written) return false;

    file = fopen(kProbePath, "r");
    if (!file) return false;
    char check[sizeof(payload)] = {};
    const size_t read = fread(check, 1, sizeof(payload) - 1, file);
    fclose(file);
    return read == sizeof(payload) - 1 &&
           memcmp(check, payload, sizeof(payload) - 1) == 0;
}

void updateSpace() {
    DWORD freeClusters = 0;
    FATFS *filesystem = nullptr;
    if (f_getfree("0:", &freeClusters, &filesystem) != FR_OK || !filesystem) {
        return;
    }
#if FF_MAX_SS != FF_MIN_SS
    const uint32_t sectorSize = filesystem->ssize;
#else
    const uint32_t sectorSize = FF_MAX_SS;
#endif
    const uint64_t clusterBytes =
        static_cast<uint64_t>(filesystem->csize) * sectorSize;
    xSemaphoreTake(gStatusMutex, portMAX_DELAY);
    gStatus.totalBytes =
        static_cast<uint64_t>(filesystem->n_fatent - 2) * clusterBytes;
    gStatus.freeBytes = static_cast<uint64_t>(freeClusters) * clusterBytes;
    xSemaphoreGive(gStatusMutex);
}

void mscEventCallback(const msc_host_event_t *event, void *) {
    if (gEvents && event) xQueueSend(gEvents, event, 0);
}

void usbLibraryTask(void *) {
    while (true) {
        uint32_t eventFlags = 0;
        usb_host_lib_handle_events(portMAX_DELAY, &eventFlags);
        if (eventFlags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) {
            usb_host_device_free_all();
        }
    }
}

void unmountDevice() {
    if (gVfs) {
        msc_host_vfs_unregister(gVfs);
        gVfs = nullptr;
    }
    if (gDevice) {
        msc_host_uninstall_device(gDevice);
        gDevice = nullptr;
    }
    xSemaphoreTake(gStatusMutex, portMAX_DELAY);
    gStatus.connected = false;
    gStatus.mounted = false;
    gStatus.writable = false;
    gStatus.totalBytes = 0;
    gStatus.freeBytes = 0;
    xSemaphoreGive(gStatusMutex);
}

void storageTask(void *) {
    usb_host_config_t hostConfig = {};
    hostConfig.intr_flags = ESP_INTR_FLAG_LEVEL1;
    esp_err_t error = usb_host_install(&hostConfig);
    if (error != ESP_OK) {
        setError("USB host install failed");
        vTaskDelete(nullptr);
        return;
    }
    xTaskCreatePinnedToCore(usbLibraryTask, "usb-lib", 4096, nullptr, 3,
                            nullptr, 0);

    msc_host_driver_config_t mscConfig = {};
    mscConfig.create_backround_task = true;
    mscConfig.task_priority = 4;
    mscConfig.stack_size = 4096;
    mscConfig.core_id = 0;
    mscConfig.callback = mscEventCallback;
    error = msc_host_install(&mscConfig);
    if (error != ESP_OK) {
        setError("USB MSC driver install failed");
        vTaskDelete(nullptr);
        return;
    }

    xSemaphoreTake(gStatusMutex, portMAX_DELAY);
    gStatus.hostReady = true;
    gStatus.error[0] = '\0';
    xSemaphoreGive(gStatusMutex);
    Serial.println("USB storage: host ready, waiting for flash drive");

    while (true) {
        msc_host_event_t event = {};
        if (xQueueReceive(gEvents, &event, portMAX_DELAY) != pdTRUE) continue;
        if (event.event == msc_host_event_t::MSC_DEVICE_DISCONNECTED) {
            Serial.println("USB storage: device disconnected");
            unmountDevice();
            continue;
        }

        unmountDevice();
        xSemaphoreTake(gStatusMutex, portMAX_DELAY);
        gStatus.connected = true;
        xSemaphoreGive(gStatusMutex);
        error = msc_host_install_device(event.device.address, &gDevice);
        if (error != ESP_OK) {
            setError("USB MSC device install failed");
            continue;
        }

        msc_host_device_info_t info = {};
        if (msc_host_get_device_info(gDevice, &info) == ESP_OK) {
            xSemaphoreTake(gStatusMutex, portMAX_DELAY);
            gStatus.vendorId = info.idVendor;
            gStatus.productId = info.idProduct;
            gStatus.sectorSize = info.sector_size;
            gStatus.sectorCount = info.sector_count;
            gStatus.capacityBytes =
                static_cast<uint64_t>(info.sector_size) * info.sector_count;
            copyWide(gStatus.product, sizeof(gStatus.product), info.iProduct);
            copyWide(gStatus.manufacturer, sizeof(gStatus.manufacturer),
                     info.iManufacturer);
            copyWide(gStatus.serial, sizeof(gStatus.serial),
                     info.iSerialNumber);
            xSemaphoreGive(gStatusMutex);
        }

        esp_vfs_fat_mount_config_t mountConfig = {};
        mountConfig.format_if_mount_failed = false;
        mountConfig.max_files = 8;
        mountConfig.allocation_unit_size = 0;
        error =
            msc_host_vfs_register(gDevice, kMountPath, &mountConfig, &gVfs);
        if (error != ESP_OK) {
            setError("USB FAT32 mount failed");
            continue;
        }

        const bool writable = verifyWritable();
        xSemaphoreTake(gStatusMutex, portMAX_DELAY);
        gStatus.mounted = true;
        gStatus.writable = writable;
        gStatus.totalBytes = gStatus.capacityBytes;
        gStatus.freeBytes = 0;
        if (!writable) {
            strncpy(gStatus.error, "USB mounted read-only or probe failed",
                    sizeof(gStatus.error) - 1);
        } else {
            gStatus.error[0] = '\0';
        }
        xSemaphoreGive(gStatusMutex);
        updateSpace();
        Serial.printf("USB storage: mounted, capacity=%llu MB writable=%s\n",
                      gStatus.capacityBytes / (1024ULL * 1024ULL),
                      writable ? "yes" : "no");
    }
}

}  // namespace

bool usbCampaignStorageBegin() {
    if (gStarted) return true;
    gStatusMutex = xSemaphoreCreateMutex();
    gEvents = xQueueCreate(4, sizeof(msc_host_event_t));
    if (!gStatusMutex || !gEvents) return false;
    gStatus = {};
    if (xTaskCreatePinnedToCore(storageTask, "usb-storage", 8192, nullptr, 3,
                                nullptr, 0) != pdPASS) {
        setError("USB storage task start failed");
        return false;
    }
    gStarted = true;
    return true;
}

UsbCampaignStorageStatus usbCampaignStorageGetStatus() {
    UsbCampaignStorageStatus status;
    if (!gStatusMutex) return status;
    xSemaphoreTake(gStatusMutex, portMAX_DELAY);
    const bool mounted = gStatus.mounted;
    xSemaphoreGive(gStatusMutex);
    if (mounted) updateSpace();
    xSemaphoreTake(gStatusMutex, portMAX_DELAY);
    status = gStatus;
    xSemaphoreGive(gStatusMutex);
    return status;
}

bool usbCampaignStorageMounted() {
    return usbCampaignStorageGetStatus().mounted;
}
