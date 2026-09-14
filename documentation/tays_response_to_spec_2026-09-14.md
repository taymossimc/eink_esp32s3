# Tay's Response to Section 13

**Response date:** 14 September 2026  
**Responding to:** *DE Instore Native E-Ink Display Integration Working Specification v0.1*  
**Scope:** Current bench prototype and firmware working tree

This response distinguishes between:

- **Confirmed:** observed on the current bench unit or directly established by
  the firmware and build configuration.
- **Implemented, validation continuing:** present in the current firmware and
  successfully built/flashed, but not yet through a complete field acceptance
  cycle.
- **Open:** requires a part-number check, measurement, production decision, or
  additional implementation.

## P0 — Exact hardware

### ESP32-S3

**Confirmed**

- Development board: Freenove ESP32-S3 WROOM-family board.
- Processor observed by the flashing tool: ESP32-S3, QFN56, revision 0.2,
  dual-core up to 240 MHz.
- Firmware target: `esp32-s3-devkitc1-n16r8`.
- Flash: 16 MB.
- PSRAM: 8 MB octal PSRAM.
- USB programming/debug connection currently enumerates as COM16.

**Open**

- The exact Freenove board SKU and Espressif module marking should be recorded
  from the physical board before the production design is frozen. The memory
  configuration is confirmed, but the module SKU should not be inferred only
  from the PlatformIO target.

### Cellular modem and GNSS

**Confirmed**

- Modem: SIMCom SIM7600G-H.
- Firmware identification: `SIM7600M11_A_V2.0.1`.
- Bench modem IMEI: `862636059550986`.
- Interface: UART1 at 115200 baud.
- ESP32 TX: GPIO10; ESP32 RX: GPIO11.
- The modem and its GNSS receiver have both been exercised successfully.
- GNSS is enabled with `AT+CGPS=1,1`; fixes are read with
  `AT+CGNSSINFO`.
- The modem requires a supply capable of at least 2 A peak. The current
  breakout is supplied from the switched 5 V system rail, not from ESP32 3.3 V.

The SIM7600G-H is an LTE Cat 4 global variant. Its published radio support is:

- LTE-FDD: B1/B2/B3/B4/B5/B7/B8/B12/B13/B18/B19/B20/B25/B26/B28/B66.
- LTE-TDD: B34/B38/B39/B40/B41.
- UMTS/HSPA+: B1/B2/B4/B5/B6/B8/B19.
- GSM/GPRS/EDGE: 850/900/1800/1900 MHz.

This includes the principal LTE bands needed for AT&T, T-Mobile, and Verizon
evaluation in the United States. SIMCom also lists AT&T, T-Mobile, U.S.
Cellular, and FCC certifications for the SIM7600G-H R2. That establishes modem
capability, but it does **not** by itself qualify the complete prototype,
antenna system, SIM plan, or enclosure for every U.S. carrier or MVNO.

Reference: [SIMCom SIM7600G-H R2 product
information](https://www.simcom.com/product/SIM7600G-H_R2.html).

### NFC

**Confirmed**

- Carrier board: MIKROE-3971 NFC Extend Click.
- Dynamic NFC tag: STMicroelectronics ST25DV16K.
- I2C user address: `0x53`; system address: `0x57`.
- ESP32 I2C pins: SDA GPIO1 and SCL GPIO2.
- GPO/interrupt: GPIO21.
- The NFC tag and GPO remain powered from 3.3 V while the switched 5 V rail is
  off.

### E-Ink display

**Confirmed**

- Display: Pimoroni Inky Impression 13.3-inch, PIM774/EL133UF1 family.
- Native panel resolution: 1600 × 1200 in landscape; firmware stores the
  physical transfer buffer as 1200 × 1600.
- Palette: black, white, yellow, red, blue, and green.
- Pixel format sent to the panel: two 4-bit pixels per byte.
- Full packed framebuffer size: 960,000 bytes.
- Interface: write-only SPI-style interface with two controller/chip-select
  regions, each covering 600 × 1600 pixels.
- Pins:
  - SCK GPIO7
  - MOSI GPIO6
  - CS0/primary GPIO8
  - CS1/secondary GPIO9
  - DC GPIO5
  - RESET GPIO17
  - BUSY GPIO18

**Open**

- Record the exact panel label, carrier-board revision, and controller IC
  markings from the physical assembly. The EL133UF1 render profile and
  dual-controller behavior are confirmed; the controller silicon part number
  is not currently identified in firmware.

### Battery and battery management

**Confirmed**

- Power module: Waveshare UPS Module 3S, SKU 23884.
- Battery configuration: three 18650 lithium-ion cells in series (3S).
- Battery-stack output range: approximately 9 V to 12.6 V.
- Reported installed-cell energy: 32.4 Wh.
- Charging input: 12.6 V at 2 A.
- The module supports charging and supplying the load at the same time.
- Regulated outputs: 5 V at up to 5 A and 3.3 V at up to 300 mA, plus the
  series battery voltage.
- Onboard INA219 monitoring provides battery voltage, current, and power over
  I2C. This capability has not yet been integrated into the ESP32 firmware.
- Onboard power/protection components include an S-8254AA battery-protection
  controller, HY2213 balancing, SY8286 5 V regulator, RT9193 3.3 V regulator,
  and overcharge, over-discharge, overcurrent, short-circuit, reverse-polarity,
  and cell-balancing protection.

Reference: [Waveshare UPS Module 3S product
information](https://www.waveshare.com/ups-module-3s.htm).

**Open — required before PCB/enclosure freeze**

- Exact installed 18650 cell manufacturer, model, rated capacity, age, and
  individual-cell protection status.
- Actual S-8254AA protection thresholds as configured on this module revision.
- How the INA219 and module I2C bus will be connected to the ESP32.
- State-of-charge estimation policy; the module measures voltage/current/power
  but does not by itself provide an accurate fuel-gauge percentage.
- Battery or board temperature sensing, which is not listed as a feature of
  this module.
- Measured brownout margin during modem transmission plus panel refresh,
  notwithstanding the module's published 5 V/5 A rating.

## P0 — Current bench wake and phone-home sequence

### Wake and power sequence

The current sequence is:

1. The ESP32 wakes from NFC EXT0, USB reset, or initial power-on.
2. GPIO4 is first driven low for three seconds. This guarantees a clean modem
   power cycle when USB resets the ESP32 while the 5 V rail was already on.
3. GPIO4 is driven high, enabling the shared switched 5 V rail for the modem
   and display.
4. The ST25DV16K is detected and its field-change GPO is configured.
5. Persistent configuration, current/next frame slots, SMS history, and
   phone-home history are loaded.
6. An open configuration access point starts as
   `EInk-Setup-<device suffix>` at `192.168.4.1`.
7. If a prepared next framebuffer exists, it is displayed before cellular
   startup.
8. The modem UART listens during the minimum 15-second modem startup window.
9. The modem initializes, reports SIM identity, imports queued SMS messages,
   and enables GNSS.
10. Internet work prefers a configured Wi-Fi network. Cellular data is used as
    fallback.
11. The device fetches and renders content on a cache miss, then downloads and
    prepares the following image.
12. After five minutes without NFC or web activity, the panel is commanded
    into deep sleep, its pins are placed in safe states, cellular data is
    released, and GPIO4 cuts the 5 V rail.
13. The ESP32 enters deep sleep with the ST25DV16K GPO as the wake source.

### Current phone-home request

The current bench endpoint is:

```text
https://digitalengine.leporia.net/ad_carousel
```

The first request lists available assets:

```http
GET /ad_carousel HTTP/1.1
Host: digitalengine.leporia.net
```

The current response is an nginx-style JSON directory listing:

```json
[
  {
    "name": "summer-promo.jpg",
    "type": "file",
    "mtime": "Thu, 10 Sep 2026 12:34:56 GMT",
    "size": 123456
  }
]
```

The firmware filters for JPEG/PNG files, excludes the current filename, chooses
the smallest eligible candidate, URL-encodes its filename, and requests it:

```http
GET /ad_carousel/summer-promo.jpg HTTP/1.1
Host: digitalengine.leporia.net
```

Every fetch result is now recorded in a bounded persistent phone-home log with
timestamp when available, trigger, endpoint, transport, fallback path, result,
HTTP status, byte count, and duration.

**Important production gap**

This is currently a read-only carousel proof of concept, not the proposed
authenticated `/device/v1/sync` contract. There is no DE-assigned device ID,
per-device bearer credential, desired/reported-state exchange, SHA-256 asset
contract, or render-confirmation POST yet. TLS clients currently use insecure
certificate mode. Production integration must replace that with server
certificate validation and per-device authentication.

## P0 — Image input and render sequence

### Accepted input

- JPEG/JPG and PNG.
- Maximum downloaded or locally uploaded source file: 3 MiB.
- JPEG EXIF orientation is applied when present.
- Configurable output orientation: 0°, 90°, 180°, or 270°.
- Images are center-scaled and crop-filled to the selected logical canvas.
- RGB pixels are quantized to the panel's six colors.
- Output is packed into a 1200 × 1600, 4-bits-per-pixel framebuffer.

The current firmware performs conversion on the ESP32-S3. DE Instore may
instead generate the exact 960,000-byte panel derivative server-side once the
render profile is formalized. If so, the device should still validate profile,
length, and cryptographic hash before accepting it.

### Persistent media behavior

- LittleFS contains CRC32-validated `current` and `next` framebuffer slots.
- Slot publication uses temporary/backup files and recovery metadata.
- On a normal tap, `next` is displayed and promoted to `current`, then a new
  `next` image is prefetched.
- The local configuration portal can accept a JPEG/PNG upload as `next`.
- A manual portal refresh displays `next` and queues the former `current`
  framebuffer for the following tap.
- Current and next media can be previewed or downloaded from the portal as
  generated indexed BMP files.
- A failed download, decode, validation, or panel refresh does not intentionally
  replace the last known good current slot.

### Panel render sequence

1. Allocate the 960,000-byte framebuffer in PSRAM.
2. Decode/normalize the source or load and CRC-check the prepared frame.
3. Initialize the panel and both controller regions.
4. Send the first 600-pixel half through CS0.
5. Send the second 600-pixel half through CS1.
6. Issue panel power-on.
7. Issue one display-refresh command.
8. Require BUSY to assert LOW, then return HIGH.
9. Fail if BUSY never asserts within 65 seconds or does not complete within
   another 65 seconds.
10. Issue panel power-off only after confirmed completion.
11. Before cutting system 5 V, issue the panel deep-sleep command.

The observed refresh duration is approximately 30 seconds under normal bench
conditions. Production temperature limits and refresh-time behavior remain to
be characterized.

## P0 — Canadian carrier and U.S. deployment

### Current validated Canadian service

- Carrier/SIM: Koodo Prepaid on the TELUS network.
- APN: `sp.koodo.com`.
- APN username/password: blank.
- PDP context: IPv4 currently used as the safe default.
- Bench SIM MSISDN: `+1 437-580-7157`.
- The modem has registered, obtained an IP address, downloaded HTTPS content,
  exposed the SIM number, and received SMS messages on this service.

### Field SIM configuration

Runtime configuration is implemented and persisted in ESP32 NVS. The open local
configuration portal exposes:

- automatic or manual APN;
- APN username and password;
- PAP/CHAP/none/automatic authentication;
- IPv4 or IPv4v6 PDP type;
- optional SIM PIN;
- roaming enable/disable;
- common APN suggestions for Koodo, AT&T, T-Mobile, and Verizon.

Automatic operator-name matching can choose common APNs, but it cannot make
every U.S. MVNO or private-IoT SIM zero-configuration. The assigned SIM provider
must supply the correct APN and authentication requirements. U.S. testing must
cover the selected SIM, antenna, enclosure, bands, attach behavior, and roaming
policy as an assembled system.

## P0 — NFC URL and wake behavior

### Confirmed electrical and firmware behavior

- The phone's RF field is detected by the ST25DV16K.
- The tag asserts its active-low FIELD_CHANGE GPO on GPIO21.
- GPIO21 is configured as the ESP32 EXT0 deep-sleep wake source.
- The ESP32 wakes and enables the switched 5 V rail.
- The NFC tag remains independently powered at 3.3 V while the modem and
  display are off.
- The same tap allows the phone to read the NDEF data already stored in the
  dynamic tag.

### URL payload status

The current firmware configures the ST25DV16K wake behavior but does not create,
change, or authenticate the NDEF URL. The exact URL currently stored on the
bench tag is therefore not represented in this repository and must be captured
directly from a phone/tag read.

For production, the NDEF URL should contain only a public device/activation
identifier or opaque short-lived token. It must not contain the device API
credential or grant unauthenticated content reassignment.

## P1 — Available telemetry

### Implemented now

- ESP32 uptime, free heap, free PSRAM, flash size, and LittleFS usage.
- Wi-Fi enabled state, selected SSID, station IP, RSSI, and configuration AP IP.
- 5 V rail state.
- Modem-ready, network-registration, and data-session states.
- Modem model/firmware string and IMEI.
- SIM ICCID, IMSI, and MSISDN when supplied by the SIM/network.
- Operator, TinyGSM signal quality/CSQ, APN configuration, and cellular IP.
- GNSS fix state, latitude, longitude, altitude, and satellite count.
- SMS sender, network timestamp, body, and persistent archive count.
- Current/next media filename, source, orientation, size, and CRC32.
- Persistent phone-home request/result history.
- Basic wake classification: NFC EXT0, power-on/reset, or other.

### Not available yet

- Battery voltage, current, and power are supported by the Waveshare 23884's
  INA219 but are not yet read by firmware. Temperature, charge state, and
  estimated SoC are also not currently available.
- A stable DE-assigned device ID and hardware revision.
- Semantic firmware version/build identifier in the sync payload.
- Complete ESP reset taxonomy such as watchdog versus brownout.
- GPS accuracy estimate and persisted fix age.
- Radio technology and engineering RSSI/RSRP/RSRQ/SINR.
- Remote desired/reported state.
- Server-confirmed render event.

## P1 — Failure, retry, and watchdog behavior

### Implemented behavior

- A cached next frame is displayed before lengthy network work.
- Wi-Fi is attempted first when enabled and configured; cellular is fallback.
- Cellular network registration waits up to 120 seconds per connection attempt.
- Carousel data connection is attempted up to three times with five seconds
  between attempts.
- Cellular HTTPS supports up to five range-resume attempts.
- A cellular body read is considered stalled after 30 seconds without data.
- The body-read budget scales with content length and is capped at 30 minutes.
- HTTP 200 is rejected when a resumed request requires HTTP 206.
- Source files above 3 MiB and malformed/unsupported images are rejected.
- Persistent frame files have length/header checks and CRC32 validation.
- Atomic temporary/backup publication protects the previous valid frame.
- Panel BUSY assertion and completion are bounded and required for render
  success.
- Queued SMS messages are archived to LittleFS and verified before their SIM
  copies are deleted, preventing the modem's 40-message store from filling.
- The panel is put to sleep before the switched 5 V rail is removed.

### Current limitations

- There is no production retry schedule or timer wake. After the device sleeps,
  a new attempt requires NFC wake, USB reset, or power-on.
- Backoff is fixed rather than exponential and does not include server-directed
  retry timing.
- Long network operations can keep the device awake for many minutes.
- There is no application-level watchdog policy or watchdog event persistence.
  ESP32 platform watchdog facilities may exist, but they have not been
  configured and accepted as part of this application.
- There is no brownout recovery policy beyond retaining the E-Ink image and
  atomic flash files.
- Error strings exist, but the production numeric error taxonomy is not yet
  defined.

## P1 — OTA status

**Status: partitioned for OTA, updater not implemented.**

The 16 MB flash layout currently reserves:

- two 5 MiB OTA application slots;
- an OTA metadata partition;
- approximately 6.16 MB LittleFS;
- NVS and coredump partitions.

No firmware download, signature verification, staged activation, health check,
rollback policy, or portal/server OTA workflow has been implemented. Signed OTA
with rollback remains required before field deployment.

## P1 — Power measurements

No defensible assembled-system current or energy results are available yet.
The following remain required:

1. Deep-sleep current measured at the battery pack with NFC wake armed.
2. Idle-awake current with the configuration AP active.
3. Modem startup and network-attach current/time.
4. Weak-signal LTE transmit peak and attach energy.
5. No-content-change wake energy.
6. Content download/decode/cache energy over Wi-Fi and cellular.
7. Full E-Ink refresh current, peak, duration, and total energy.
8. Combined worst case: cellular transmission during display operation.
9. Charging and low-battery behavior.

Measurements should be captured as time-series voltage/current traces, not
only handheld-meter spot values.

## P2 — Prototype PCB and enclosure constraints

### Current prototype constraints

- This is a development-board and breakout-board bench assembly, not the final
  custom PCB.
- Display and modem share a switched 5 V rail controlled by ESP32 GPIO4.
- NFC must retain 3.3 V power during ESP32 deep sleep.
- The modem needs high peak current and a clean power-cycle interval.
- LTE and GNSS antennas require separation, ground-plane review, and enclosure
  placement testing.
- NFC antenna position and nearby metal/battery/display layers will materially
  affect phone interaction.
- The physical SIM must remain serviceable unless an eSIM design is selected.
- USB programming/recovery access is currently important and should not be
  removed until OTA and rollback are proven.
- GPIO12–GPIO16 were intentionally avoided because they overlap the Freenove
  camera connector.
- E-Ink BUSY, RESET, both chip selects, and the switched-rail enable should
  remain accessible as production test points.

### Not yet locked

- Production MCU module and PCB revision.
- Exact installed 18650 cell model/capacity and any additional downstream
  charger/regulator components beyond the Waveshare 23884.
- SIM versus eSIM.
- Antenna parts and placement.
- External connector and charging arrangement.
- Enclosure dimensions, service access, environmental rating, and thermal
  assumptions.
- Manufacturing provisioning and end-of-line test interface.

## Repository package for DE Instore

The current implementation evidence is available in:

- `platformio.ini` — MCU target, dependencies, pins, and build flags.
- `partitions_16mb_cache.csv` — flash/OTA/LittleFS layout.
- `inky_ESP32_13inch/inky_ESP32_13inch.ino` — complete wake/content/sleep
  orchestration.
- `inky_ESP32_13inch/modem/` — SIM7600 startup, SIM/SMS/GNSS, APN, cellular
  data, and HTTPS.
- `inky_ESP32_13inch/nfc/` — ST25DV16K field-change wake path.
- `inky_ESP32_13inch/power/` — switched rail and safe shutdown.
- `inky_ESP32_13inch/display/` and `EPD_13in3e.*` — panel transfer and
  BUSY-confirmed refresh.
- `inky_ESP32_13inch/image/` — JPEG/PNG normalization and six-color packing.
- `inky_ESP32_13inch/cache/` — persistent current/next frame store.
- `inky_ESP32_13inch/config/` and `portal/` — field configuration and local
  diagnostics.
- `inky_ESP32_13inch/network/` — Wi-Fi-first/cellular-fallback transport.
- `inky_ESP32_13inch/sms/` — persistent SMS history.
- `inky_ESP32_13inch/log/` — persistent phone-home history.
- `documentation/image_carousel.md` — current endpoint contract and examples.
- `hardware/photos/` — available prototype photographs.

## Immediate items Tay still needs to supply or measure

1. Photograph/transcription of the exact ESP32 module, display label/revision,
   installed 18650 cell models, any power components downstream of the
   Waveshare 23884, modem breakout, and antenna parts.
2. The exact NDEF URL currently read by a phone.
3. Complete power-path diagram and INA219-to-ESP32 integration details.
4. Deep-sleep, modem attach, weak-signal, and display-refresh energy traces.
5. One selected U.S. carrier/MVNO SIM and its official APN/profile for assembled
   system testing.
6. Decision on the canonical DE device ID and manufacturing provisioning path.
7. Production sync/authentication/content-integrity contract.
8. OTA signing, health-check, and rollback design.
9. Application watchdog, retry/backoff, and autonomous timer-wake policy.
10. Temperature-range and 72-hour soak results.

