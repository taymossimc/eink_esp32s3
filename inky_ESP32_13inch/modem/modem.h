#pragma once

#include <Arduino.h>

struct ModemDiagnostics {
    bool ready = false;
    bool networkRegistered = false;
    bool dataConnected = false;
    bool gnssFixed = false;
    int signalQuality = 0;
    int satellites = 0;
    float latitude = 0;
    float longitude = 0;
    float altitude = 0;
    char model[128] = {};
    char imei[24] = {};
    char phoneNumber[32] = {};
    char iccid[32] = {};
    char imsi[32] = {};
    char operatorName[48] = {};
    char cellularIp[48] = {};
};

ModemDiagnostics modemGetDiagnostics();

// Start modem UART immediately after 5V enable (before RDY may arrive).
void modemStartUartEarly();

// Listen for RDY during the minimum modem power-up window.
bool modemListenDuringPowerUp();

// Bring up TinyGSM. Waits for modem RDY when not already handled.
bool modemBegin();

// Enable GNSS using SIM7600M11 CGPS commands (not CGNSSPWR).
bool modemEnableGnss();

// Read a fix via AT+CGNSSINFO; returns true when fix mode is 2 or 3.
bool modemGetGnssFix(float *lat, float *lon, float *alt = nullptr,
                     int *usat = nullptr, int *vsat = nullptr);

void modemPrintStatus();
void modemPrintGnss();
void modemPrintSimNumber();

// Poll and print unread SMS messages queued while the modem was powered off.
bool modemPollQueuedSms();

// Register, set APN, connect GPRS/LTE. Caller should modemDisconnectData() when done.
bool modemConnectData();
void modemDisconnectData();

// Forward stray modem URCs to USB serial.
void modemPumpSerial();

// Cellular data session (stay connected while awake).
bool modemEnsureDataConnected();
bool modemIsDataConnected();
void modemReleaseData();

// HTTPS GET — allocates body in PSRAM; caller must free with heap_caps_free().
bool modemHttpsGet(const char *host, const char *path,
                   uint8_t **bodyOut, size_t *lenOut,
                   size_t maxBytes = 3UL * 1024UL * 1024UL);

bool modemBandwidthTest(size_t bytes, float *mbps, uint32_t *durationMs);
