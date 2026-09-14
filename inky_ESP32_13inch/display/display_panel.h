#pragma once

#include <Arduino.h>

// Show a packed 6-color framebuffer (1200x1600, 2 pixels/byte).
bool displayShowPackedImage(const uint8_t *data, size_t len);

// Put an initialized panel controller into deep sleep while 5 V is still on.
// Safe to call when the panel has not been initialized.
void displaySleepBeforePowerOff();
