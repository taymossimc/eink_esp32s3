#pragma once

// Waveshare UPS Module 3S (23884) INA219 on a dedicated I2C bus.
#ifndef UPS_BATTERY_SDA
#define UPS_BATTERY_SDA 14
#endif
#ifndef UPS_BATTERY_SCL
#define UPS_BATTERY_SCL 15
#endif

// Typical INA219 address on Waveshare UPS boards; probed at runtime.
#ifndef UPS_BATTERY_I2C_ADDR
#define UPS_BATTERY_I2C_ADDR 0x42
#endif
