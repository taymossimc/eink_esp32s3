#pragma once

#include <Arduino.h>

namespace tmads {

bool identityBegin();
bool identityIsEnrolled();
const char *identityDeviceId();
const uint8_t *identityPrivateKey();
const uint8_t *identityPublicKey();
String identityPublicKeyWire();
String identityBootId();

void identitySetEnrollmentToken(const char *token);

uint32_t identityWakeIntervalSec();
void identitySetWakeIntervalSec(uint32_t seconds);
uint32_t identityAwakeTimeoutSec();
void identitySetAwakeTimeoutSec(uint32_t seconds);
uint32_t identityAwakePollSec();
void identitySetAwakePollSec(uint32_t seconds);
void identitySetNextWakeAt(const char *serverTime, const char *nextWakeAt);
void identitySetLowBatteryOverride(bool enabled);
bool identityScheduleTimerWake();
bool identityConsumedOtaRebootFlag();
void identityMarkOtaRebootPending();

}  // namespace tmads
