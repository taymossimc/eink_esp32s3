#pragma once

#include <Arduino.h>

bool nfcBegin();
bool nfcConfigureWake();
bool nfcWriteUrl(const char *url);
void nfcOnWake();
void nfcAttachTapMonitor();
void nfcPollTapMonitor();
bool nfcConsumeTapRequest();
void nfcSignalTapRequest();
bool nfcEnterDeepSleep();
