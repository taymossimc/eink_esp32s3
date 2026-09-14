#pragma once

#include <Arduino.h>

void powerRailInit();
void powerRailSet(bool on);
bool powerRailIsOn();

void activityBump();
uint32_t activityIdleMs();
bool activityTimedOut();

// Drive display SPI/control pins low before cutting the 5 V rail.
void displayPinsSafe();
