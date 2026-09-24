#pragma once

#include <Arduino.h>

struct UsbCampaignStorageStatus {
    bool hostReady = false;
    bool connected = false;
    bool mounted = false;
    bool writable = false;
    uint16_t vendorId = 0;
    uint16_t productId = 0;
    uint32_t sectorSize = 0;
    uint32_t sectorCount = 0;
    uint64_t capacityBytes = 0;
    uint64_t totalBytes = 0;
    uint64_t freeBytes = 0;
    char product[48] = {};
    char manufacturer[48] = {};
    char serial[48] = {};
    char error[96] = {};
};

bool usbCampaignStorageBegin();
UsbCampaignStorageStatus usbCampaignStorageGetStatus();
bool usbCampaignStorageMounted();
