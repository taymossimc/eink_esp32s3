#pragma once

#include <Arduino.h>

// Portrait panel: 1200 x 1600, crop-fill, 6-color packed (960000 bytes).
// outBuffer must hold EPD_13IN3E_WIDTH * EPD_13IN3E_HEIGHT / 2 bytes.
bool imageDecodeCropFill(const uint8_t *data, size_t len, const char *filename,
                         uint8_t *outBuffer, size_t outCapacity,
                         size_t *outLen, uint16_t orientation = 0);
