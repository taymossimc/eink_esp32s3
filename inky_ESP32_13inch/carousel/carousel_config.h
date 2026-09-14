#pragma once

#define CAROUSEL_HOST "digitalengine.leporia.net"
#define CAROUSEL_INDEX_PATH "/ad_carousel"
#define CAROUSEL_BASE_URL "https://digitalengine.leporia.net/ad_carousel"

// Reject downloads larger than this (PSRAM buffer limit).
#define CAROUSEL_MAX_DOWNLOAD_BYTES (3UL * 1024UL * 1024UL)
