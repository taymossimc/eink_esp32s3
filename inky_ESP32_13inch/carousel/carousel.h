#pragma once

#include <Arduino.h>

// Mount the persistent next-frame cache.
bool carouselCacheBegin();

// Display the prepared next frame and promote it to current.
bool carouselDisplayCached();

// Manual portal refresh: display next and queue the old current frame as next.
bool carouselDisplayCachedAndSwap();

// Fetch, decode, and atomically store the next ready-to-display frame.
bool carouselFetchAndCache();

// Fetch a carousel image and refresh the panel immediately (cache-miss path).
bool carouselFetchAndDisplay();

const char *carouselCurrentImageName();
