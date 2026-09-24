#pragma once

#define TMADS_HOST "digitalengine.leporia.net"
#define TMADS_API_PREFIX "/device"
#define TMADS_CHECKIN_PATH_TEMPLATE "/device/api/v1/edge/%s/check-in"
#define TMADS_ENROLL_SIGN_PATH "/api/v1/devices/enroll"
#define TMADS_ENROLL_REQUEST_PATH "/device/api/v1/devices/enroll"
#define TMADS_FIRMWARE_PATH_TEMPLATE "/device/api/v1/edge/%s/firmware/%s"

// Pinned Digital Engine edge signing identity. Signed responses also carry
// the exact RFC 8785 payload bytes used by the server, allowing the ESP32 to
// verify them without implementing JSON canonicalization locally.
#define TMADS_EDGE_SIGNER_KEY_ID "ed25519:8d49a023f133b5b9f9a0ffdb"
#define TMADS_EDGE_PUBLIC_KEY_B64 "sxcn35gI2DK5JBlyAl-m2FZl0p1LwpbsL-e9fKAV9lk"

// Default wake interval when the server has not assigned one yet (13 hours).
#define TMADS_DEFAULT_WAKE_INTERVAL_SEC 46800U

// Maximum firmware download size (must fit OTA partition).
#define TMADS_MAX_FIRMWARE_BYTES (5242880U)
