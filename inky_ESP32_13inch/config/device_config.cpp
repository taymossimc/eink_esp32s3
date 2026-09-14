#include "device_config.h"

#include <Preferences.h>
#include <ctype.h>
#include <string.h>

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace device_config {
namespace {

constexpr char kPreferencesNamespace[] = "device_cfg";
constexpr char kPreferencesKey[] = "config";
constexpr uint32_t kBlobMagic = 0x44434647UL;  // "DCFG"
constexpr uint16_t kBlobVersion = 1;

struct __attribute__((packed)) PersistentWifiProfile {
  uint8_t enabled;
  char ssid[kMaxSsidLength + 1];
  char password[kMaxWifiPasswordLength + 1];
};

struct __attribute__((packed)) PersistentPayloadV1 {
  uint8_t wifiEnabled;
  PersistentWifiProfile wifiProfiles[kMaxWifiProfiles];
  uint8_t cellularAutoApn;
  char cellularApn[kMaxCellularFieldLength + 1];
  char cellularUsername[kMaxCellularFieldLength + 1];
  char cellularPassword[kMaxCellularFieldLength + 1];
  uint8_t cellularAuth;
  uint8_t cellularPdp;
  char simPin[kMaxSimPinLength + 1];
  uint8_t roamingAllowed;
  uint16_t displayOrientation;
};

struct __attribute__((packed)) PersistentBlobV1 {
  uint32_t magic;
  uint16_t version;
  uint16_t payloadSize;
  uint32_t payloadCrc;
  PersistentPayloadV1 payload;
};

SemaphoreHandle_t configMutex() {
  static StaticSemaphore_t storage;
  static SemaphoreHandle_t mutex = xSemaphoreCreateMutexStatic(&storage);
  return mutex;
}

class MutexLock {
 public:
  MutexLock() { xSemaphoreTake(configMutex(), portMAX_DELAY); }
  ~MutexLock() { xSemaphoreGive(configMutex()); }

  MutexLock(const MutexLock &) = delete;
  MutexLock &operator=(const MutexLock &) = delete;
};

template <size_t N>
void copyText(char (&destination)[N], const char *source) {
  if (source == nullptr) {
    destination[0] = '\0';
    memset(destination + 1, 0, N - 1);
    return;
  }

  size_t length = 0;
  while (length < N - 1 && source[length] != '\0') {
    ++length;
  }
  memcpy(destination, source, length);
  destination[length] = '\0';
  memset(destination + length + 1, 0, N - length - 1);
}

template <size_t N>
void canonicalizeText(char (&text)[N]) {
  text[N - 1] = '\0';
  size_t length = 0;
  while (length < N && text[length] != '\0') {
    ++length;
  }
  if (length + 1 < N) {
    memset(text + length + 1, 0, N - length - 1);
  }
}

template <size_t N>
void sanitizeAtText(char (&text)[N]) {
  canonicalizeText(text);
  size_t write = 0;
  for (size_t read = 0; text[read] != '\0' && write < N - 1; ++read) {
    const unsigned char c = static_cast<unsigned char>(text[read]);
    if (c < 0x20 || c == 0x7F || c == '"') continue;
    text[write++] = static_cast<char>(c);
  }
  text[write] = '\0';
  if (write + 1 < N) memset(text + write + 1, 0, N - write - 1);
}

uint32_t crc32(const uint8_t *data, size_t length) {
  uint32_t crc = 0xFFFFFFFFUL;
  for (size_t i = 0; i < length; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xEDB88320UL & (0UL - (crc & 1UL)));
    }
  }
  return ~crc;
}

DeviceConfig &cachedConfig() {
  static DeviceConfig config = defaults();
  return config;
}

void toPersistent(const DeviceConfig &config, PersistentPayloadV1 &payload) {
  memset(&payload, 0, sizeof(payload));
  payload.wifiEnabled = config.wifiEnabled ? 1 : 0;
  for (size_t i = 0; i < kMaxWifiProfiles; ++i) {
    payload.wifiProfiles[i].enabled =
        config.wifiProfiles[i].enabled ? 1 : 0;
    memcpy(payload.wifiProfiles[i].ssid, config.wifiProfiles[i].ssid,
           sizeof(payload.wifiProfiles[i].ssid));
    memcpy(payload.wifiProfiles[i].password, config.wifiProfiles[i].password,
           sizeof(payload.wifiProfiles[i].password));
  }
  payload.cellularAutoApn = config.cellularAutoApn ? 1 : 0;
  memcpy(payload.cellularApn, config.cellularApn, sizeof(payload.cellularApn));
  memcpy(payload.cellularUsername, config.cellularUsername,
         sizeof(payload.cellularUsername));
  memcpy(payload.cellularPassword, config.cellularPassword,
         sizeof(payload.cellularPassword));
  payload.cellularAuth = static_cast<uint8_t>(config.cellularAuth);
  payload.cellularPdp = static_cast<uint8_t>(config.cellularPdp);
  memcpy(payload.simPin, config.simPin, sizeof(payload.simPin));
  payload.roamingAllowed = config.roamingAllowed ? 1 : 0;
  payload.displayOrientation =
      static_cast<uint16_t>(config.displayOrientation);
}

DeviceConfig fromPersistent(const PersistentPayloadV1 &payload) {
  DeviceConfig config{};
  config.wifiEnabled = payload.wifiEnabled != 0;
  for (size_t i = 0; i < kMaxWifiProfiles; ++i) {
    config.wifiProfiles[i].enabled =
        payload.wifiProfiles[i].enabled != 0;
    memcpy(config.wifiProfiles[i].ssid, payload.wifiProfiles[i].ssid,
           sizeof(config.wifiProfiles[i].ssid));
    memcpy(config.wifiProfiles[i].password,
           payload.wifiProfiles[i].password,
           sizeof(config.wifiProfiles[i].password));
  }
  config.cellularAutoApn = payload.cellularAutoApn != 0;
  memcpy(config.cellularApn, payload.cellularApn, sizeof(config.cellularApn));
  memcpy(config.cellularUsername, payload.cellularUsername,
         sizeof(config.cellularUsername));
  memcpy(config.cellularPassword, payload.cellularPassword,
         sizeof(config.cellularPassword));
  config.cellularAuth = static_cast<CellularAuth>(payload.cellularAuth);
  config.cellularPdp = static_cast<PdpType>(payload.cellularPdp);
  memcpy(config.simPin, payload.simPin, sizeof(config.simPin));
  config.roamingAllowed = payload.roamingAllowed != 0;
  config.displayOrientation =
      static_cast<DisplayOrientation>(payload.displayOrientation);
  sanitize(config);
  return config;
}

bool writePersistent(const DeviceConfig &config) {
  PersistentBlobV1 blob{};
  blob.magic = kBlobMagic;
  blob.version = kBlobVersion;
  blob.payloadSize = sizeof(blob.payload);
  toPersistent(config, blob.payload);
  blob.payloadCrc =
      crc32(reinterpret_cast<const uint8_t *>(&blob.payload),
            sizeof(blob.payload));

  Preferences preferences;
  if (!preferences.begin(kPreferencesNamespace, false)) {
    return false;
  }
  const size_t written =
      preferences.putBytes(kPreferencesKey, &blob, sizeof(blob));
  preferences.end();
  return written == sizeof(blob);
}

bool equalsIgnoreCase(const char *text, const char *expected) {
  if (text == nullptr) {
    return false;
  }
  while (isspace(static_cast<unsigned char>(*text))) {
    ++text;
  }
  while (*text != '\0' && *expected != '\0' &&
         tolower(static_cast<unsigned char>(*text)) ==
             tolower(static_cast<unsigned char>(*expected))) {
    ++text;
    ++expected;
  }
  while (isspace(static_cast<unsigned char>(*text))) {
    ++text;
  }
  return *text == '\0' && *expected == '\0';
}

}  // namespace

DeviceConfig defaults() {
  DeviceConfig config{};
  config.wifiEnabled = true;
  config.cellularAutoApn = true;
  copyText(config.cellularApn, "sp.koodo.com");
  config.cellularAuth = CellularAuth::Auto;
  config.cellularPdp = PdpType::IPv4;
  config.roamingAllowed = false;
  config.displayOrientation = DisplayOrientation::Portrait;
  return config;
}

void sanitize(DeviceConfig &config) {
  for (size_t i = 0; i < kMaxWifiProfiles; ++i) {
    canonicalizeText(config.wifiProfiles[i].ssid);
    canonicalizeText(config.wifiProfiles[i].password);
    if (config.wifiProfiles[i].ssid[0] == '\0') {
      config.wifiProfiles[i].enabled = false;
    }
  }

  sanitizeAtText(config.cellularApn);
  sanitizeAtText(config.cellularUsername);
  sanitizeAtText(config.cellularPassword);
  sanitizeAtText(config.simPin);

  switch (config.cellularAuth) {
    case CellularAuth::Auto:
    case CellularAuth::None:
    case CellularAuth::PAP:
    case CellularAuth::CHAP:
      break;
    default:
      config.cellularAuth = CellularAuth::Auto;
      break;
  }

  switch (config.cellularPdp) {
    case PdpType::IPv4:
    case PdpType::IPv4v6:
      break;
    default:
      config.cellularPdp = PdpType::IPv4;
      break;
  }

  bool validPin = config.simPin[0] == '\0';
  if (!validPin) {
    size_t length = 0;
    while (config.simPin[length] != '\0' &&
           isdigit(static_cast<unsigned char>(config.simPin[length]))) {
      ++length;
    }
    validPin = config.simPin[length] == '\0' && length >= 4 &&
               length <= kMaxSimPinLength;
  }
  if (!validPin) {
    memset(config.simPin, 0, sizeof(config.simPin));
  }

  switch (config.displayOrientation) {
    case DisplayOrientation::Portrait:
    case DisplayOrientation::Landscape:
    case DisplayOrientation::PortraitInverted:
    case DisplayOrientation::LandscapeInverted:
      break;
    default:
      config.displayOrientation = DisplayOrientation::Portrait;
      break;
  }
}

bool load() {
  MutexLock lock;
  DeviceConfig loaded = defaults();

  Preferences preferences;
  if (!preferences.begin(kPreferencesNamespace, false)) {
    cachedConfig() = loaded;
    return false;
  }

  const size_t storedSize = preferences.getBytesLength(kPreferencesKey);
  if (storedSize != sizeof(PersistentBlobV1)) {
    preferences.end();
    cachedConfig() = loaded;
    writePersistent(loaded);
    return false;
  }

  PersistentBlobV1 blob{};
  const size_t read =
      preferences.getBytes(kPreferencesKey, &blob, sizeof(blob));
  preferences.end();

  const bool headerValid =
      read == sizeof(blob) && blob.magic == kBlobMagic &&
      blob.version == kBlobVersion &&
      blob.payloadSize == sizeof(blob.payload);
  const bool crcValid =
      headerValid &&
      blob.payloadCrc ==
          crc32(reinterpret_cast<const uint8_t *>(&blob.payload),
                sizeof(blob.payload));
  if (!crcValid) {
    cachedConfig() = loaded;
    return false;
  }

  cachedConfig() = fromPersistent(blob.payload);
  return true;
}

DeviceConfig getSnapshot() {
  MutexLock lock;
  return cachedConfig();
}

bool save(const DeviceConfig &config) {
  MutexLock lock;
  DeviceConfig candidate = config;
  sanitize(candidate);
  if (!writePersistent(candidate)) {
    return false;
  }
  cachedConfig() = candidate;
  return true;
}

bool updateAndSave(ConfigUpdater updater, void *context) {
  if (updater == nullptr) {
    return false;
  }

  MutexLock lock;
  DeviceConfig candidate = cachedConfig();
  updater(candidate, context);
  sanitize(candidate);
  if (!writePersistent(candidate)) {
    return false;
  }
  cachedConfig() = candidate;
  return true;
}

const char *toString(CellularAuth value) {
  switch (value) {
    case CellularAuth::Auto:
      return "Auto";
    case CellularAuth::None:
      return "None";
    case CellularAuth::PAP:
      return "PAP";
    case CellularAuth::CHAP:
      return "CHAP";
    default:
      return "Auto";
  }
}

const char *toString(PdpType value) {
  switch (value) {
    case PdpType::IPv4:
      return "IPv4";
    case PdpType::IPv4v6:
      return "IPv4v6";
    default:
      return "IPv4v6";
  }
}

const char *toString(DisplayOrientation value) {
  switch (value) {
    case DisplayOrientation::Portrait:
      return "0";
    case DisplayOrientation::Landscape:
      return "90";
    case DisplayOrientation::PortraitInverted:
      return "180";
    case DisplayOrientation::LandscapeInverted:
      return "270";
    default:
      return "0";
  }
}

bool parseCellularAuth(const char *text, CellularAuth &value) {
  if (equalsIgnoreCase(text, "auto")) {
    value = CellularAuth::Auto;
  } else if (equalsIgnoreCase(text, "none")) {
    value = CellularAuth::None;
  } else if (equalsIgnoreCase(text, "pap")) {
    value = CellularAuth::PAP;
  } else if (equalsIgnoreCase(text, "chap")) {
    value = CellularAuth::CHAP;
  } else {
    return false;
  }
  return true;
}

bool parsePdpType(const char *text, PdpType &value) {
  if (equalsIgnoreCase(text, "ipv4")) {
    value = PdpType::IPv4;
  } else if (equalsIgnoreCase(text, "ipv4v6") ||
             equalsIgnoreCase(text, "ipv4/ipv6")) {
    value = PdpType::IPv4v6;
  } else {
    return false;
  }
  return true;
}

bool parseDisplayOrientation(const char *text, DisplayOrientation &value) {
  if (equalsIgnoreCase(text, "0") || equalsIgnoreCase(text, "portrait")) {
    value = DisplayOrientation::Portrait;
  } else if (equalsIgnoreCase(text, "90") ||
             equalsIgnoreCase(text, "landscape")) {
    value = DisplayOrientation::Landscape;
  } else if (equalsIgnoreCase(text, "180") ||
             equalsIgnoreCase(text, "portrait-inverted")) {
    value = DisplayOrientation::PortraitInverted;
  } else if (equalsIgnoreCase(text, "270") ||
             equalsIgnoreCase(text, "landscape-inverted")) {
    value = DisplayOrientation::LandscapeInverted;
  } else {
    return false;
  }
  return true;
}

}  // namespace device_config
