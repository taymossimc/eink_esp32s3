#pragma once

// SIM7600G-H UART wiring (crossed — modem talks to ESP32, not USB host)
//
//   Modem 5/12V  -> external 5 V supply (>= 2 A peak; do NOT use ESP32 3V3)
//   Modem GND    -> ESP32 GND (common ground)
//   Modem TX     -> ESP32 GPIO11 (ESP32 RX)
//   Modem RX     -> ESP32 GPIO10 (ESP32 TX)
//
// GPIO10/11 are free on the Freenove board and do not overlap the Inky pins
// (5–9, 17–18). Keep the modem USB unplugged when using UART to the ESP32.
//
// This USB-dongle breakout likely level-shifts UART to 3.3 V. If AT never
// responds, verify logic levels before connecting directly.

#define MODEM_UART_NUM       1
#define MODEM_RX_PIN         11   // connect to modem TX
#define MODEM_TX_PIN         10   // connect to modem RX
#define MODEM_BAUD           115200

// Set your carrier APN before testing mobile data:
// Verified on the same SIM7600G-H + Koodo SIM in NFC Plus (modem_integration/modem_profile.md)
#define CELLULAR_APN         "sp.koodo.com"
#define CELLULAR_USER        ""
#define CELLULAR_PASS        ""
