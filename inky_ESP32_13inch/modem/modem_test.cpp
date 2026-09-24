#include <Arduino.h>
#include "modem_config.h"

#define TINY_GSM_MODEM_SIM7600
#define TINY_GSM_RX MODEM_RX_PIN
#define TINY_GSM_TX MODEM_TX_PIN
#include <TinyGsmClient.h>

HardwareSerial SerialModem(MODEM_UART_NUM);
TinyGsm modem(SerialModem);

static void drainModem(uint32_t ms = 200) {
    const uint32_t end = millis() + ms;
    while (millis() < end) {
        while (SerialModem.available()) {
            Serial.write(SerialModem.read());
        }
        delay(1);
    }
}

static bool waitForModemBoot(uint32_t timeoutMs = 60000) {
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

static bool initModemWithRetry(int attempts = 5) {
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

static bool sendAT(const char *cmd, uint32_t timeoutMs = 3000) {
    Serial.printf(">> %s\n", cmd);
    SerialModem.print(cmd);
    SerialModem.print("\r\n");

    String line;
    const uint32_t end = millis() + timeoutMs;
    while (millis() < end) {
        while (SerialModem.available()) {
            const char c = static_cast<char>(SerialModem.read());
            Serial.write(c);
            if (c == '\n') {
                if (line.indexOf("OK") >= 0 || line.indexOf("ERROR") >= 0) {
                    return line.indexOf("OK") >= 0;
                }
                line = "";
            } else if (c != '\r') {
                line += c;
            }
        }
        delay(1);
    }
    Serial.println("<< (timeout)");
    return false;
}

static void testGnssRaw() {
    Serial.println("\n--- GNSS (raw AT) ---");
    sendAT("AT+CGNSSPWR=1", 5000);
    delay(2000);
    sendAT("AT+CGNSSINFO", 5000);
}

static void testGnssTinyGsm() {
#if defined(TINY_GSM_MODEM_HAS_GPS)
    Serial.println("\n--- GNSS (TinyGSM) ---");
    if (!modem.enableGPS()) {
        Serial.println("enableGPS failed");
        return;
    }
    delay(15000);

    float lat = 0, lon = 0, speed = 0, alt = 0, accuracy = 0;
    int vsat = 0, usat = 0;
    int year = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0;

    if (modem.getGPS(&lat, &lon, &speed, &alt, &vsat, &usat, &accuracy,
                     &year, &month, &day, &hour, &minute, &second)) {
        Serial.printf("Fix: %.6f, %.6f  alt=%.1f  sats=%d/%d\n",
                      lat, lon, alt, usat, vsat);
    } else {
        Serial.println("No fix yet. Move antenna outdoors and retry.");
        Serial.print("Raw: ");
        Serial.println(modem.getGPSraw());
    }
#else
    Serial.println("TinyGSM GPS helpers not enabled for this build.");
#endif
}

static void testCellularData() {
    Serial.println("\n--- Cellular data ---");
    Serial.println("Waiting for network registration...");
    if (!modem.waitForNetwork(120000L)) {
        Serial.println("Network registration failed (SIM/antenna/APN?)");
        return;
    }
    Serial.printf("Registered. Operator: %s  CSQ: %d\n",
                  modem.getOperator().c_str(), modem.getSignalQuality());

    if (strcmp(CELLULAR_APN, "YOUR_APN") != 0) {
        String apnCmd = String("AT+CGDCONT=1,\"IP\",\"") + CELLULAR_APN + "\"";
        sendAT(apnCmd.c_str(), 5000);
        Serial.printf("Connecting GPRS/LTE APN: %s\n", CELLULAR_APN);
        if (!modem.gprsConnect(CELLULAR_APN, CELLULAR_USER, CELLULAR_PASS)) {
            Serial.println("gprsConnect failed");
            return;
        }
        Serial.printf("Connected. IP: %s\n", modem.getLocalIP().c_str());
        modem.gprsDisconnect();
    } else {
        Serial.println("Set CELLULAR_APN in modem_config.h to test mobile data.");
    }
}

void setup() {
    Serial.begin(115200);
    delay(2000);
    Serial.println("\nSIM7600G-H modem bring-up (ESP32-S3)");
    Serial.printf("UART%d TX=GPIO%d RX=GPIO%d @ %d baud\n",
                  MODEM_UART_NUM, MODEM_TX_PIN, MODEM_RX_PIN, MODEM_BAUD);

    SerialModem.begin(MODEM_BAUD, SERIAL_8N1, MODEM_RX_PIN, MODEM_TX_PIN);
    waitForModemBoot(60000);

    Serial.println("\n--- Modem identity ---");
    if (!initModemWithRetry()) {
        Serial.println("TinyGSM init failed — check wiring (swap TX/RX?) and power.");
        sendAT("AT");
        sendAT("AT+CPIN?");
        sendAT("AT+CSQ");
        return;
    }

    Serial.printf("Modem: %s %s\n", modem.getModemName().c_str(),
                  modem.getModemInfo().c_str());
    Serial.printf("IMEI: %s\n", modem.getIMEI().c_str());

    testGnssRaw();
    testGnssTinyGsm();
    testCellularData();

    Serial.println("\nDone. Reboot to rerun tests.");
}

void loop() {
    while (SerialModem.available()) {
        Serial.write(SerialModem.read());
    }
    delay(10);
}
