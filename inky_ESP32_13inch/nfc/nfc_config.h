#pragma once

// MIKROE-3971 NFC Extend Click (ST25DV16K) on I2C.
// INT/GPO must stay powered from 3.3 V for wake-on-tap while the 5 V rail is off.
#ifndef NFC_SDA
#define NFC_SDA 1
#endif
#ifndef NFC_SCL
#define NFC_SCL 2
#endif
#ifndef NFC_INT
#define NFC_INT 21
#endif

// High-side 5 V enable: HIGH = rail ON (display + modem).
#ifndef POWER_ENABLE_GPIO
#define POWER_ENABLE_GPIO 4
#endif

// Deep sleep after this many ms without an NFC tap.
#ifndef IDLE_SLEEP_MS
#define IDLE_SLEEP_MS (5UL * 60UL * 1000UL)
#endif
