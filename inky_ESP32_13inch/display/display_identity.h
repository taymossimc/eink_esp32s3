#pragma once

#include <Arduino.h>

struct DisplayIdentity {
    bool present = false;
    uint16_t width = 0;
    uint16_t height = 0;
    uint8_t color = 0;
    uint8_t pcbVariant = 0;
    uint8_t displayVariant = 0;
};

bool displayIdentityProbe();
DisplayIdentity displayIdentityGet();
