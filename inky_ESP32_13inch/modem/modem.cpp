#include "modem.h"
#include "modem_config.h"
#include "config/device_config.h"
#include "power/power.h"
#include "sms/sms_store.h"

#include <TinyGsmClient.h>
#include <SSLClient.h>
#include <ArduinoHttpClient.h>
#include <esp_heap_caps.h>
#include <sys/time.h>
#include <time.h>

namespace {

HardwareSerial SerialModem(MODEM_UART_NUM);
TinyGsm modem(SerialModem);
TinyGsmClient gsmClient(modem);
bool gDataConnected = false;
bool gModemBootWaitDone = false;
bool gModemUartStarted = false;
ModemDiagnostics gDiagnostics;

void drainModem(uint32_t ms = 200) {
    const uint32_t end = millis() + ms;
    while (millis() < end) {
        while (SerialModem.available()) {
            Serial.write(SerialModem.read());
        }
        delay(1);
    }
}

bool sendATOk(const char *cmd, String *response = nullptr, uint32_t timeoutMs = 5000,
              bool echo = true) {
    if (echo) {
        Serial.printf(">> %s\n", cmd);
    }
    SerialModem.print(cmd);
    SerialModem.print("\r\n");

    String line;
    String lastInfo;
    bool ok = false;
    const uint32_t end = millis() + timeoutMs;
    while (millis() < end) {
        while (SerialModem.available()) {
            const char c = static_cast<char>(SerialModem.read());
            if (echo) {
                Serial.write(c);
            }
            if (c == '\n') {
                line.trim();
                if (line.length() > 0 && line != "OK" && line != "ERROR") {
                    lastInfo = line;
                }
                if (line == "OK") {
                    ok = true;
                    if (response) {
                        *response = lastInfo;
                    }
                    return true;
                }
                if (line == "ERROR") {
                    if (response) {
                        *response = lastInfo;
                    }
                    return false;
                }
                line = "";
            } else if (c != '\r') {
                line += c;
            }
        }
        delay(1);
    }
    if (echo) {
        Serial.println("<< (timeout)");
    }
    if (response) {
        response->clear();
    }
    return false;
}

bool sendATCollect(const char *cmd, String *response,
                   uint32_t timeoutMs = 15000, bool echo = true) {
    if (!response) return false;
    response->clear();
    response->reserve(8192);
    if (echo) Serial.printf(">> %s\n", cmd);
    SerialModem.print(cmd);
    SerialModem.print("\r\n");

    String line;
    const uint32_t end = millis() + timeoutMs;
    while (millis() < end) {
        while (SerialModem.available()) {
            const char c = static_cast<char>(SerialModem.read());
            if (echo) Serial.write(c);
            if (c == '\n') {
                line.trim();
                if (line == "OK") return true;
                if (line == "ERROR") return false;
                if (line.length() && line != cmd) {
                    *response += line;
                    *response += '\n';
                }
                line = "";
            } else if (c != '\r') {
                line += c;
            }
        }
        delay(1);
    }
    if (echo) Serial.println("<< (timeout)");
    return false;
}

int hexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

void appendUtf8(String &out, uint16_t cp) {
    if (cp < 0x80) {
        out += static_cast<char>(cp);
    } else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
}

String decodeSmsBody(const String &input) {
    String text = input;
    text.trim();
    if (text.length() < 4 || (text.length() % 4) != 0) return text;
    for (size_t i = 0; i < text.length(); ++i) {
        if (hexNibble(text[i]) < 0) return text;
    }
    String decoded;
    decoded.reserve(text.length() / 2);
    for (size_t i = 0; i + 3 < text.length(); i += 4) {
        const uint16_t cp =
            static_cast<uint16_t>((hexNibble(text[i]) << 12) |
                                  (hexNibble(text[i + 1]) << 8) |
                                  (hexNibble(text[i + 2]) << 4) |
                                  hexNibble(text[i + 3]));
        if (cp == 0xFEFF) continue;
        appendUtf8(decoded, cp);
    }
    return decoded;
}

String quotedField(const String &line, int wanted) {
    int field = 0;
    bool inQuote = false;
    int start = -1;
    for (int i = 0; i < line.length(); ++i) {
        if (line[i] != '"') continue;
        if (!inQuote) {
            inQuote = true;
            start = i + 1;
        } else {
            if (field == wanted) return line.substring(start, i);
            ++field;
            inQuote = false;
        }
    }
    return "";
}

bool archiveSmsResponse(const String &response) {
    SmsMessage current = {};
    int indices[40] = {};
    size_t indexCount = 0;
    bool haveCurrent = false;
    bool allStored = true;

    auto finishCurrent = [&]() {
        if (!haveCurrent) return;
        current.id = smsMessageFingerprint(current);
        if (smsStoreAppend(current)) {
            for (size_t i = 0; i < indexCount; ++i) {
                char cmd[24] = {};
                snprintf(cmd, sizeof(cmd), "AT+CMGD=%d", indices[i]);
                sendATOk(cmd, nullptr, 5000, false);
            }
            Serial.printf("SMS archived: %s %s (%u part%s)\n",
                          current.sender, current.timestamp,
                          static_cast<unsigned>(indexCount),
                          indexCount == 1 ? "" : "s");
        } else {
            Serial.println("SMS: archive write failed; leaving SIM copy");
            allStored = false;
        }
        current = {};
        indexCount = 0;
        haveCurrent = false;
    };

    int position = 0;
    while (position < response.length()) {
        int newline = response.indexOf('\n', position);
        if (newline < 0) newline = response.length();
        String line = response.substring(position, newline);
        line.trim();
        position = newline + 1;
        if (!line.length()) continue;

        if (line.startsWith("+CMGL:")) {
            const int colon = line.indexOf(':');
            const int comma = line.indexOf(',', colon + 1);
            const int index = line.substring(colon + 1, comma).toInt();
            const String sender = quotedField(line, 1);
            const String timestamp = quotedField(line, 3);
            if (haveCurrent &&
                (sender != current.sender || timestamp != current.timestamp)) {
                finishCurrent();
            }
            if (!haveCurrent) {
                strncpy(current.sender, sender.c_str(),
                        sizeof(current.sender) - 1);
                strncpy(current.timestamp, timestamp.c_str(),
                        sizeof(current.timestamp) - 1);
                haveCurrent = true;
            }
            if (indexCount < 40) indices[indexCount++] = index;
            continue;
        }

        if (haveCurrent) {
            const String decoded = decodeSmsBody(line);
            const size_t used = strnlen(current.body, sizeof(current.body));
            const size_t available = sizeof(current.body) - used - 1;
            if (decoded.length() > available) current.incomplete = true;
            strncat(current.body, decoded.c_str(), available);
        }
    }
    finishCurrent();
    return allStored;
}

bool waitForModemBoot(uint32_t timeoutMs = 60000) {
    Serial.printf("Waiting up to %lus for modem boot (RDY)...\n", timeoutMs / 1000);
    String line;
    const uint32_t end = millis() + timeoutMs;
    while (millis() < end) {
        while (SerialModem.available()) {
            const char c = static_cast<char>(SerialModem.read());
            Serial.write(c);
            if (c == '\n') {
                if (line.indexOf("RDY") >= 0) {
                    Serial.println("(modem RDY — settling)");
                    delay(3000);
                    drainModem(2000);
                    return true;
                }
                line = "";
            } else if (c != '\r') {
                line += c;
            }
        }
        delay(10);
    }
    Serial.println("No RDY seen — will try init anyway.");
    return false;
}

bool initModemWithRetry(int attempts = 5) {
    for (int i = 0; i < attempts; i++) {
        if (modem.init()) {
            return true;
        }
        Serial.printf("init attempt %d/%d failed, retry in 3s...\n", i + 1, attempts);
        delay(3000);
        drainModem(500);
    }
    return false;
}

void syncSystemTimeFromModem() {
    String response;
    if (!sendATOk("AT+CCLK?", &response, 5000, false)) return;
    const int quote = response.indexOf('"');
    if (quote < 0) return;
    int yy = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0;
    int tzQuarters = 0;
    if (sscanf(response.c_str() + quote + 1, "%d/%d/%d,%d:%d:%d%d",
               &yy, &month, &day, &hour, &minute, &second,
               &tzQuarters) != 7) {
        return;
    }
    struct tm local = {};
    local.tm_year = 100 + yy;
    local.tm_mon = month - 1;
    local.tm_mday = day;
    local.tm_hour = hour;
    local.tm_min = minute;
    local.tm_sec = second;
    local.tm_isdst = 0;
    const time_t utc = mktime(&local) - tzQuarters * 15 * 60;
    if (utc <= 0) return;
    struct timeval now = {utc, 0};
    settimeofday(&now, nullptr);
}

bool cgpsEnabled() {
    String resp;
    if (!sendATOk("AT+CGPS?", &resp, 5000)) {
        return false;
    }
    // +CGPS: <status>[,<mode>]
    const int idx = resp.indexOf("+CGPS:");
    if (idx < 0) {
        return false;
    }
    const int status = resp.substring(idx + 7).toInt();
    return status == 1;
}

float nmeaToDecimal(float nmea, char hemisphere) {
    const int degrees = static_cast<int>(nmea / 100.0f);
    const float minutes = nmea - degrees * 100.0f;
    float decimal = degrees + minutes / 60.0f;
    if (hemisphere == 'S' || hemisphere == 'W') {
        decimal = -decimal;
    }
    return decimal;
}

bool parseCgnssInfo(const String &info, float *lat, float *lon, float *alt,
                      int *usat, int *vsat) {
    // +CGNSSINFO: mode,gp,gl,bd,lat,N/S,lon,E/W,date,time,alt,speed,...
    const int idx = info.indexOf("+CGNSSINFO:");
    if (idx < 0) {
        return false;
    }

    String payload = info.substring(idx + 12);
    payload.trim();

    int field = 0;
    int fixMode = 0;
    float latNmea = 0;
    char latHem = 'N';
    float lonNmea = 0;
    char lonHem = 'E';
    float altitude = 0;
    int gpsSats = 0;

    int start = 0;
    while (start <= payload.length()) {
        const int comma = payload.indexOf(',', start);
        const String token =
            (comma < 0) ? payload.substring(start) : payload.substring(start, comma);

        switch (field) {
            case 0:
                fixMode = token.toInt();
                break;
            case 1:
                gpsSats = token.toInt();
                break;
            case 4:
                latNmea = token.toFloat();
                break;
            case 5:
                if (token.length() > 0) {
                    latHem = token.charAt(0);
                }
                break;
            case 6:
                lonNmea = token.toFloat();
                break;
            case 7:
                if (token.length() > 0) {
                    lonHem = token.charAt(0);
                }
                break;
            case 10:
                altitude = token.toFloat();
                break;
            default:
                break;
        }

        if (comma < 0) {
            break;
        }
        start = comma + 1;
        field++;
    }

    if (fixMode != 2 && fixMode != 3) {
        return false;
    }

    if (lat) {
        *lat = nmeaToDecimal(latNmea, latHem);
    }
    if (lon) {
        *lon = nmeaToDecimal(lonNmea, lonHem);
    }
    if (alt) {
        *alt = altitude;
    }
    if (usat) {
        *usat = gpsSats;
    }
    if (vsat) {
        *vsat = gpsSats;
    }
    return true;
}

}  // namespace

void modemStartUartEarly() {
    Serial.printf("Modem UART%d TX=GPIO%d RX=GPIO%d @ %d baud\n",
                  MODEM_UART_NUM, MODEM_TX_PIN, MODEM_RX_PIN, MODEM_BAUD);
    SerialModem.begin(MODEM_BAUD, SERIAL_8N1, MODEM_RX_PIN, MODEM_TX_PIN);
    gModemUartStarted = true;
}

bool modemListenDuringPowerUp() {
    Serial.println("Listening for modem RDY (min 15 s after 5V enable)...");
    const uint32_t start = millis();
    String line;
    bool rdySeen = false;

    while (millis() - start < 60000) {
        while (SerialModem.available()) {
            const char c = static_cast<char>(SerialModem.read());
            Serial.write(c);
            if (c == '\n') {
                if (line.indexOf("RDY") >= 0) {
                    rdySeen = true;
                }
                line = "";
            } else if (c != '\r') {
                line += c;
            }
        }

        if (rdySeen && (millis() - start) >= 15000) {
            Serial.println("(modem RDY — settling)");
            delay(3000);
            drainModem(2000);
            gModemBootWaitDone = true;
            return true;
        }

        if (!rdySeen && (millis() - start) >= 45000) {
            break;
        }
        delay(10);
    }

    const uint32_t elapsed = millis() - start;
    if (elapsed < 15000) {
        delay(15000 - elapsed);
    }
    return rdySeen;
}

bool modemBegin() {
    if (!gModemUartStarted) {
        modemStartUartEarly();
    }
    if (!gModemBootWaitDone) {
        waitForModemBoot(60000);
    }
    gModemBootWaitDone = false;

    Serial.println("--- Modem init ---");
    if (!initModemWithRetry()) {
        Serial.println("Modem init failed — check wiring and power.");
        sendATOk("AT");
        sendATOk("AT+CPIN?");
        sendATOk("AT+CSQ");
        return false;
    }

    const auto config = device_config::getSnapshot();
    if (config.simPin[0] != '\0' && modem.getSimStatus() != SIM_READY) {
        Serial.println("Modem: unlocking configured SIM PIN");
        if (!modem.simUnlock(config.simPin)) {
            Serial.println("Modem: SIM PIN unlock failed");
            return false;
        }
    }

    const String model = modem.getModemInfo();
    const String imei = modem.getIMEI();
    const String iccid = modem.getSimCCID();
    const String imsi = modem.getIMSI();
    strncpy(gDiagnostics.model, model.c_str(),
            sizeof(gDiagnostics.model) - 1);
    strncpy(gDiagnostics.imei, imei.c_str(),
            sizeof(gDiagnostics.imei) - 1);
    strncpy(gDiagnostics.iccid, iccid.c_str(),
            sizeof(gDiagnostics.iccid) - 1);
    strncpy(gDiagnostics.imsi, imsi.c_str(),
            sizeof(gDiagnostics.imsi) - 1);
    gDiagnostics.ready = true;
    syncSystemTimeFromModem();
    Serial.printf("Modem: %s\n", model.c_str());
    Serial.printf("IMEI: %s\n", imei.c_str());
    return true;
}

bool modemEnableGnss() {
    Serial.println("--- GNSS enable ---");

    if (cgpsEnabled()) {
        Serial.println("GNSS already enabled (+CGPS: 1)");
    } else {
        // SIM7600M11 rejects CGNSSPWR; use CGPS=1,1 like the Pi device-agent.
        if (!sendATOk("AT+CGPS=1,1", nullptr, 10000) || !cgpsEnabled()) {
            Serial.println("GNSS enable failed (AT+CGPS=1,1)");
            return false;
        }
        Serial.println("GNSS enabled (AT+CGPS=1,1)");
    }

    sendATOk("AT+CGPSAUTO=1", nullptr, 5000);
    return true;
}

bool modemGetGnssFix(float *lat, float *lon, float *alt, int *usat, int *vsat) {
    String info;
    if (!sendATOk("AT+CGNSSINFO", &info, 5000)) {
        return false;
    }
    const bool fixed = parseCgnssInfo(info, lat, lon, alt, usat, vsat);
    gDiagnostics.gnssFixed = fixed;
    if (fixed) {
        gDiagnostics.latitude = *lat;
        gDiagnostics.longitude = *lon;
        gDiagnostics.altitude = alt ? *alt : 0;
        gDiagnostics.satellites = usat ? *usat : 0;
    }
    return fixed;
}

void modemPrintGnss() {
    float lat = 0, lon = 0, alt = 0;
    int usat = 0, vsat = 0;
    if (modemGetGnssFix(&lat, &lon, &alt, &usat, &vsat)) {
        Serial.printf("GNSS fix: %.6f, %.6f  alt=%.1fm  sats=%d\n",
                      lat, lon, alt, usat);
    } else {
        Serial.println("No GNSS fix yet.");
    }
}

void modemPrintSimNumber() {
    Serial.println("--- SIM phone number ---");
    String info;
    if (sendATOk("AT+CNUM", &info, 8000)) {
        if (info.length() > 0) {
            Serial.printf("CNUM: %s\n", info.c_str());
            const int firstQuote = info.indexOf('"', info.indexOf(',') + 1);
            const int lastQuote =
                firstQuote >= 0 ? info.indexOf('"', firstQuote + 1) : -1;
            if (firstQuote >= 0 && lastQuote > firstQuote) {
                const String number =
                    info.substring(firstQuote + 1, lastQuote);
                strncpy(gDiagnostics.phoneNumber, number.c_str(),
                        sizeof(gDiagnostics.phoneNumber) - 1);
            }
        } else {
            Serial.println("CNUM: (empty — number not stored on SIM)");
        }
    } else {
        Serial.println("CNUM query failed");
    }
}

bool modemPollQueuedSms() {
    Serial.println("--- Queued SMS poll ---");

    if (!modem.waitForNetwork(60000L)) {
        Serial.println("SMS: network registration unavailable");
        return false;
    }

    // Allow multipart messages queued while off to finish reaching SIM storage.
    delay(8000);

    if (!sendATOk("AT+CMGF=1", nullptr, 5000)) {
        Serial.println("SMS: unable to enable text mode");
        return false;
    }

    if (!sendATOk("AT+CPMS=\"SM\",\"SM\",\"SM\"", nullptr, 5000)) {
        Serial.println("SMS: unable to select SIM message storage");
        return false;
    }
    sendATOk("AT+CPMS?", nullptr, 5000);

    String response;
    if (!sendATCollect("AT+CMGL=\"ALL\"", &response, 20000)) {
        Serial.println("SMS: message poll failed");
        return false;
    }

    if (response.indexOf("+CMGL:") < 0) {
        Serial.println("SMS: no queued messages");
    } else {
        if (!archiveSmsResponse(response)) {
            return false;
        }
        Serial.println("SMS: queued messages archived");
    }

    // Report future arrivals while awake as +CMTI URCs. They will be read on
    // the next wake poll if not handled before shutdown.
    sendATOk("AT+CNMI=2,1,0,0,0", nullptr, 5000);
    return true;
}

ModemDiagnostics modemGetDiagnostics() {
    gDiagnostics.dataConnected = gDataConnected;
    return gDiagnostics;
}

void modemPrintStatus() {
    Serial.println("--- Modem status ---");
    if (!modem.waitForNetwork(30000L)) {
        Serial.println("Not registered on network.");
        return;
    }
    Serial.printf("Operator: %s  CSQ: %d\n",
                  modem.getOperator().c_str(), modem.getSignalQuality());
    modemPrintGnss();
}

bool modemConnectData() {
    Serial.println("--- Cellular data ---");
    if (!modem.waitForNetwork(120000L)) {
        Serial.println("Network registration failed.");
        return false;
    }
    gDiagnostics.networkRegistered = true;
    const String operatorName = modem.getOperator();
    strncpy(gDiagnostics.operatorName, operatorName.c_str(),
            sizeof(gDiagnostics.operatorName) - 1);
    gDiagnostics.signalQuality = modem.getSignalQuality();

    const auto config = device_config::getSnapshot();
    String registration;
    if (sendATOk("AT+CEREG?", &registration, 5000, false)) {
        const int firstComma = registration.indexOf(',');
        const int secondComma =
            firstComma >= 0 ? registration.indexOf(',', firstComma + 1) : -1;
        const int status =
            firstComma >= 0
                ? registration.substring(firstComma + 1, secondComma).toInt()
                : 0;
        if (status == 5 && !config.roamingAllowed) {
            Serial.println("Cellular data blocked: roaming is disabled");
            return false;
        }
    }
    String apn = config.cellularApn;
    if (config.cellularAutoApn) {
        const String carrier = modem.getOperator();
        String upper = carrier;
        upper.toUpperCase();
        if (upper.indexOf("T-MOBILE") >= 0 ||
            upper.indexOf("TMOBILE") >= 0) {
            apn = "fast.t-mobile.com";
        } else if (upper.indexOf("AT&T") >= 0 ||
                   upper.indexOf("ATT") >= 0) {
            apn = "broadband";
        } else if (upper.indexOf("VERIZON") >= 0) {
            apn = "vzwinternet";
        } else if (upper.indexOf("KOODO") >= 0 ||
                   upper.indexOf("TELUS") >= 0) {
            apn = "sp.koodo.com";
        }
    }
    if (!apn.length()) apn = CELLULAR_APN;
    const char *pdp =
        config.cellularPdp == device_config::PdpType::IPv4v6 ? "IPV4V6" : "IP";
    String apnCmd = String("AT+CGDCONT=1,\"") + pdp + "\",\"" + apn + "\"";
    sendATOk(apnCmd.c_str(), nullptr, 5000);
    if (config.cellularAuth != device_config::CellularAuth::Auto) {
        int auth = 0;
        if (config.cellularAuth == device_config::CellularAuth::PAP) auth = 1;
        if (config.cellularAuth == device_config::CellularAuth::CHAP) auth = 2;
        String authCmd = String("AT+CGAUTH=1,") + auth;
        if (auth != 0) {
            authCmd += String(",\"") + config.cellularUsername + "\",\"" +
                       config.cellularPassword + "\"";
        }
        sendATOk(authCmd.c_str(), nullptr, 5000);
    }

    Serial.printf("Connecting APN: %s\n", apn.c_str());
    if (!modem.gprsConnect(apn.c_str(), config.cellularUsername,
                           config.cellularPassword)) {
        Serial.println("gprsConnect failed");
        gDataConnected = false;
        return false;
    }

    String ip = modem.getLocalIP();
    ip.replace("+IPADDR: ", "");
    Serial.printf("Connected. IP: %s\n", ip.c_str());
    strncpy(gDiagnostics.cellularIp, ip.c_str(),
            sizeof(gDiagnostics.cellularIp) - 1);
    gDataConnected = true;
    gDiagnostics.dataConnected = true;
    return true;
}

void modemDisconnectData() {
    modem.gprsDisconnect();
    gDataConnected = false;
    gDiagnostics.dataConnected = false;
}

bool modemEnsureDataConnected() {
    if (gDataConnected && modem.isGprsConnected()) {
        return true;
    }
    return modemConnectData();
}

bool modemIsDataConnected() {
    return gDataConnected && modem.isGprsConnected();
}

void modemReleaseData() {
    if (gDataConnected) {
        modemDisconnectData();
    }
}

static uint32_t httpReadDeadlineMs(int contentLength) {
    // Large PNG/JPG over LTE can be slow; allow up to 30 min for multi-MB bodies.
    uint32_t budget = 120000;
    if (contentLength > 0) {
        const uint32_t scaled =
            static_cast<uint32_t>(contentLength / 256) * 1000U;
        if (scaled > budget) {
            budget = scaled;
        }
        if (budget > 1800000U) {
            budget = 1800000U;
        }
    }
    return budget;
}

static bool readHttpBody(HttpClient &http, int contentLength, uint8_t *buffer,
                         size_t maxBytes, size_t *outLen) {
    *outLen = 0;
    const uint32_t deadline = millis() + httpReadDeadlineMs(contentLength);

    if (contentLength >= 0) {
        const int len = min(contentLength, static_cast<int>(maxBytes));
        if (len <= 0) {
            return false;
        }

        uint8_t *writePtr = buffer;
        int remaining = len;
        uint32_t idleSince = millis();
        size_t lastProgress = *outLen;
        while (remaining > 0 && millis() < deadline) {
            const int chunk = http.readBytes(
                writePtr, static_cast<size_t>(min(remaining, 8192)));
            if (chunk <= 0) {
                if (millis() - idleSince > 30000) {
                    Serial.printf("HTTPS read stalled at %u/%d bytes\n",
                                  static_cast<unsigned>(*outLen), len);
                    break;
                }
                modemPumpSerial();
                delay(10);
                continue;
            }
            idleSince = millis();
            writePtr += chunk;
            remaining -= chunk;
            *outLen += static_cast<size_t>(chunk);
            if (*outLen - lastProgress >= 65536) {
                Serial.printf("HTTPS download %u/%d bytes\n",
                              static_cast<unsigned>(*outLen), len);
                lastProgress = *outLen;
                activityBump();
            }
        }
        if (*outLen != static_cast<size_t>(len)) {
            Serial.printf("HTTPS incomplete body: %u/%d bytes\n",
                          static_cast<unsigned>(*outLen), len);
            return false;
        }
        return true;
    }

    // Chunked / unknown length — read until idle, not until maxBytes.
    uint32_t lastDataMs = millis();
    while (*outLen < maxBytes && millis() < deadline) {
        const int avail = http.available();
        if (avail > 0) {
            const size_t toRead =
                min(static_cast<size_t>(avail), maxBytes - *outLen);
            const int got = http.readBytes(buffer + *outLen, toRead);
            if (got > 0) {
                *outLen += static_cast<size_t>(got);
                lastDataMs = millis();
            }
        } else if (millis() - lastDataMs > 2000) {
            break;
        } else {
            delay(10);
        }
    }

    return *outLen > 0;
}

bool modemHttpsGet(const char *host, const char *path,
                   uint8_t **bodyOut, size_t *lenOut, size_t maxBytes) {
    if (!bodyOut || !lenOut) {
        return false;
    }
    *bodyOut = nullptr;
    *lenOut = 0;

    if (!modemEnsureDataConnected()) {
        Serial.println("HTTPS: data not connected");
        return false;
    }

    int expectedLength = -1;
    uint8_t *buffer = nullptr;
    size_t allocSize = maxBytes;
    size_t totalReceived = 0;

    for (int attempt = 0; attempt < 5; ++attempt) {
        if (attempt > 0) {
            Serial.printf("HTTPS resume attempt %d from byte %u\n", attempt + 1,
                          static_cast<unsigned>(totalReceived));
            delay(2000);
            activityBump();
        }

        SSLClient secureClient(&gsmClient);
        secureClient.setInsecure();

        HttpClient http(secureClient, host, 443);
        http.setTimeout(300000);

        Serial.printf("HTTPS GET https://%s%s\n", host, path);
        int err = 0;
        if (totalReceived > 0) {
            http.beginRequest();
            err = http.get(path);
            char rangeHdr[32] = {};
            snprintf(rangeHdr, sizeof(rangeHdr), "bytes=%u-",
                     static_cast<unsigned>(totalReceived));
            http.sendHeader("Range", rangeHdr);
            http.endRequest();
        } else {
            err = http.get(path);
        }
        if (err != 0) {
            Serial.printf("HTTPS request failed: %d\n", err);
            http.stop();
            continue;
        }

        const int status = http.responseStatusCode();
        const int contentLength = http.contentLength();
        Serial.printf("HTTPS status=%d length=%d\n", status, contentLength);

        if ((totalReceived == 0 && status != 200) ||
            (totalReceived > 0 && status != 206)) {
            Serial.printf("HTTPS unexpected status %d for offset %u\n", status,
                          static_cast<unsigned>(totalReceived));
            http.stop();
            continue;
        }

        if (expectedLength < 0) {
            if (contentLength > 0) {
                expectedLength = contentLength;
            } else if (status == 206 && totalReceived > 0) {
                expectedLength = static_cast<int>(totalReceived + contentLength);
            }
        }

        if (expectedLength > static_cast<int>(maxBytes)) {
            Serial.printf("HTTPS body too large (%d > %u)\n", expectedLength,
                            static_cast<unsigned>(maxBytes));
            http.stop();
            heap_caps_free(buffer);
            return false;
        }

        if (!buffer) {
            allocSize = (expectedLength > 0)
                            ? static_cast<size_t>(expectedLength)
                            : maxBytes;
            buffer = static_cast<uint8_t *>(
                heap_caps_malloc(allocSize, MALLOC_CAP_SPIRAM));
            if (!buffer) {
                buffer = static_cast<uint8_t *>(malloc(allocSize));
            }
            if (!buffer) {
                Serial.println("HTTPS: out of memory");
                http.stop();
                return false;
            }
        }

        size_t received = 0;
        const int bodyLength =
            (contentLength > 0) ? contentLength
                                : ((expectedLength > 0)
                                       ? expectedLength -
                                             static_cast<int>(totalReceived)
                                       : -1);
        const bool bodyComplete =
            readHttpBody(http, bodyLength, buffer + totalReceived,
                         allocSize - totalReceived, &received);
        http.stop();
        totalReceived += received;
        if (received > 0) {
            activityBump();
        }

        if (!bodyComplete) {
            Serial.printf("HTTPS: retained partial body (%u bytes total)\n",
                          static_cast<unsigned>(totalReceived));
            continue;
        }

        if (expectedLength > 0 &&
            totalReceived >= static_cast<size_t>(expectedLength)) {
            break;
        }
        if (expectedLength < 0 && received > 0) {
            break;
        }
    }

    if (!buffer || totalReceived == 0) {
        heap_caps_free(buffer);
        return false;
    }

    if (expectedLength > 0 &&
        totalReceived != static_cast<size_t>(expectedLength)) {
        Serial.printf("HTTPS incomplete after retries: %u/%d bytes\n",
                      static_cast<unsigned>(totalReceived), expectedLength);
        heap_caps_free(buffer);
        return false;
    }

    *bodyOut = buffer;
    *lenOut = totalReceived;
    Serial.printf("HTTPS received %u bytes\n", static_cast<unsigned>(totalReceived));
    return true;
}

bool modemBandwidthTest(size_t bytes, float *mbps, uint32_t *durationMs) {
    if (!mbps || !durationMs || bytes < 1024 ||
        bytes > 5U * 1024U * 1024U) {
        return false;
    }
    char path[64] = {};
    snprintf(path, sizeof(path), "/__down?bytes=%u",
             static_cast<unsigned>(bytes));
    uint8_t *body = nullptr;
    size_t received = 0;
    const uint32_t start = millis();
    const bool ok = modemHttpsGet("speed.cloudflare.com", path, &body,
                                  &received, bytes);
    *durationMs = millis() - start;
    if (body) heap_caps_free(body);
    if (!ok || !*durationMs) return false;
    *mbps = static_cast<float>(received) * 8.0f /
            (static_cast<float>(*durationMs) * 1000.0f);
    return true;
}

void modemPumpSerial() {
    while (SerialModem.available()) {
        Serial.write(SerialModem.read());
    }
}
