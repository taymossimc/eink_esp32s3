#pragma once

#include <Arduino.h>

bool nfcBegin();
bool nfcConfigureWake();
void nfcOnWake();
void nfcAttachTapMonitor();
void nfcPollTapMonitor();
bool nfcConsumeTapRequest();
void nfcSignalTapRequest();
bool nfcEnterDeepSleep();
