#pragma once

#include <Arduino.h>

void powerRailInit();
void powerRailSet(bool on);
bool powerRailIsOn();

void activityBump();
uint32_t activityIdleMs();
bool activityTimedOut();

// Runtime awake timeout (defaults to IDLE_SLEEP_MS until configured).
void activitySetTimeoutMs(uint32_t timeoutMs);
uint32_t activityTimeoutMs();

// Drive display SPI/control pins low before cutting the 5 V rail.
void displayPinsSafe();
