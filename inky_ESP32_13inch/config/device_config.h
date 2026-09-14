#pragma once

#include <Arduino.h>

namespace device_config {

constexpr size_t kMaxWifiProfiles = 8;
constexpr size_t kMaxSsidLength = 32;
constexpr size_t kMaxWifiPasswordLength = 64;
constexpr size_t kMaxCellularFieldLength = 63;
constexpr size_t kMaxSimPinLength = 8;

enum class CellularAuth : uint8_t {
  Auto = 0,
  None,
  PAP,
  CHAP,
};

enum class PdpType : uint8_t {
  IPv4 = 0,
  IPv4v6,
};

enum class DisplayOrientation : uint16_t {
  Portrait = 0,
  Landscape = 90,
  PortraitInverted = 180,
  LandscapeInverted = 270,
};

struct WifiProfile {
  bool enabled;
  char ssid[kMaxSsidLength + 1];
  char password[kMaxWifiPasswordLength + 1];
};

struct DeviceConfig {
  bool wifiEnabled;
  WifiProfile wifiProfiles[kMaxWifiProfiles];

  bool cellularAutoApn;
  char cellularApn[kMaxCellularFieldLength + 1];
  char cellularUsername[kMaxCellularFieldLength + 1];
  char cellularPassword[kMaxCellularFieldLength + 1];
  CellularAuth cellularAuth;
  PdpType cellularPdp;
  char simPin[kMaxSimPinLength + 1];
  bool roamingAllowed;

  DisplayOrientation displayOrientation;
};

using ConfigUpdater = void (*)(DeviceConfig &config, void *context);

// Loads the cached configuration from NVS. Missing, corrupt, or unsupported
// data is replaced in memory with defaults. Returns true only for a valid load.
bool load();

// Returns a thread-safe copy of the current cached configuration.
DeviceConfig getSnapshot();

// Sanitizes, atomically stores, and then publishes a complete configuration.
// The previous cached value remains active if the NVS write fails.
bool save(const DeviceConfig &config);

// Applies an update and saves it while holding the module mutex, preventing
// lost updates between tasks. The callback must not call this module's API.
bool updateAndSave(ConfigUpdater updater, void *context = nullptr);

// Returns a fresh configuration with documented defaults.
DeviceConfig defaults();

// Enforces string termination, enum validity, and profile/PIN constraints.
void sanitize(DeviceConfig &config);

const char *toString(CellularAuth value);
const char *toString(PdpType value);
const char *toString(DisplayOrientation value);

bool parseCellularAuth(const char *text, CellularAuth &value);
bool parsePdpType(const char *text, PdpType &value);
bool parseDisplayOrientation(const char *text, DisplayOrientation &value);

}  // namespace device_config
