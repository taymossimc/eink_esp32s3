**DIGITAL ENGINE INSTORE**

**Native E-Ink Display
Integration Working Specification**

Hardware / Firmware / Cloud Contract / Field Workflow

|  |  |
| --- | --- |
| **Document status** | Working draft for joint review |
| **Version** | 0.1 |
| **Date** | 10 September 2026 |
| **Primary hardware contact** | Tay |
| **Purpose** | Define what DE Instore needs from the native display before PCB / enclosure / field-pilot decisions are frozen. |

*This is intentionally a working document. Fill in the Notes / Answer columns during review; mark decisions only when both hardware and software implications are understood.*

# 1. Purpose and Design Intent

The objective is to establish a first-party DE Instore display path that we control end-to-end, while continuing to support third-party display suppliers through separate adapters. Tay's prototype changes the software problem materially: the display can wake, identify itself, contact our server, retrieve content, render it, report state, and return to low-power operation without a Raspberry Pi or N100-class host.

This document separates three things that should not be conflated: (1) what Tay has already demonstrated, (2) what DE Instore recommends as the production contract, and (3) what still needs an answer, measurement, or joint decision before hardware / firmware are frozen.

|  |
| --- |
| **Guiding principle:** Keep the device simple and deterministic. Put campaign logic, image preparation, scheduling policy, audit history, user workflow, and provider abstraction in DE Instore. The device should primarily identify, synchronize, download a panel-ready asset, render, confirm, report health, and sleep. |

# 2. Baseline Reported by Tay - Treat as Confirmed for Review

| **State** | **Area** | **Current understanding** | **Notes / verify** |
| --- | --- | --- | --- |
| [x] | Compute | Single ESP32-S3 microprocessor; no Raspberry Pi or N100 required for current demonstrated functionality. | Confirm exact module / flash / PSRAM configuration. |
| [x] | Battery | 3-cell battery pack, 32.4 Wh, with integrated battery management. | Need topology, chemistry, protection, charging and measured consumption. |
| [x] | NFC | NFC wake-on-tap plus URL delivery to the user's phone. | Need exact NFC implementation, payload, security model and tap sequence. |
| [x] | Cellular / GPS | 3G/LTE modem and GPS receiver integrated and bench validated. | Need exact modem, LTE category/bands, carrier/APN and U.S. qualification. |
| [x] | E-Ink | Display power is switched on wake; image is converted to compatible format and pushed to the E-Ink display. | Need panel/controller/profile and exact conversion/render location. |
| [x] | End-to-end | Bench flow works end-to-end. | Define acceptance tests and telemetry so DE Instore can verify the same flow remotely. |
| [ ] | Next build | Prototype-style single PCB, enclosure integration, shipment. | Use the gates below before design choices become costly to change. |

# 3. Decision Gates

| **Gate** | **Must be resolved before** | **Minimum output** |
| --- | --- | --- |
| G1 | PCB layout freeze | Device identity/provisioning; modem + U.S. LTE compatibility; NFC wake circuitry; power telemetry; programming/debug access; antenna constraints; OTA feasibility. |
| G2 | Enclosure freeze | NFC antenna location; LTE/GPS antenna placement; battery access/service; SIM access or eSIM decision; charging/power connector; thermal/weather assumptions; labels/serial/QR. |
| G3 | Firmware field-candidate | Device-server sync contract; retry/offline behavior; last-known-good content; confirmation events; secure credentials; OTA update/rollback; watchdog; logging. |
| G4 | U.S. shipment / pilot | U.S. SIM/APN working; LTE bands verified; cold/weak-signal tests; real battery measurements; first DE Instore server check-in; first remotely assigned content; failure recovery. |
| G5 | Production candidate | Manufacturing provisioning, per-device keys, serial scheme, production test fixture, regulatory/carrier requirements, service procedure, fleet observability and update policy. |

# 4. Tay Review Checklist - Hardware and Device Behavior

Status convention: [x] reported complete; [ ] open / verify; [D] joint decision; [T] requires measured test. The Notes / Answer column is intentionally wide enough to write into during a call or bench review.

## 4.1 Device identity and provisioning

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [D] | What is the canonical DE Instore device ID? | Recommend a DE-assigned immutable serial independent of MAC, IMEI and panel serial. |  |
| [ ] | What identifiers are available in hardware/firmware? | ESP32 MAC, modem IMEI, SIM ICCID/eSIM ID, panel serial, board revision. |  |
| [D] | How is a new unit provisioned? | Recommend factory/bench step that assigns device ID + per-device credential and registers it with DE Instore. |  |
| [ ] | Can the unit expose board revision and hardware BOM revision in every check-in? | Needed for fleet diagnosis and firmware targeting. |  |
| [ ] | How are replacement modem, SIM, panel or PCB identities handled? | DE device identity should survive serviceable-part replacement where appropriate. |  |
| [ ] | Is there a physical serial/QR label? | Recommend human-readable short ID + QR as fallback to NFC. |  |

## 4.2 Power, battery and wake behavior

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [ ] | Battery chemistry, series/parallel topology, nominal/full/cutoff voltage? | Document exact pack specification and BMS thresholds. |  |
| [ ] | What battery telemetry is measurable? | Voltage, estimated SoC %, charge state, current if available, temperature if available. |  |
| [T] | Deep-sleep current at battery pack? | Measure complete assembled electronics, not only ESP32 datasheet current. |  |
| [T] | Wake-cycle energy: modem attach + server sync + image download + full E-Ink refresh? | Measure Wh/mAh per representative cycle. |  |
| [T] | Peak current during modem transmit and E-Ink refresh? | Verify regulator/BMS margin and no brownout under weak cellular conditions. |  |
| [D] | Wake sources? | At minimum: NFC and RTC/timer. Consider power-on/service button as a maintenance wake. |  |
| [D] | Default unattended wake interval? | Needed so content can update without a technician tap. Must be policy-configurable. |  |
| [ ] | Can the server change the next wake interval? | Recommend bounded server control with safe min/max values. |  |
| [ ] | Behavior below low-battery threshold? | Recommend preserve current image, reduce wake frequency, report low battery, avoid risky refresh/OTA. |  |
| [ ] | Charging/power input and field service method? | Document connector, voltage/current, charge time and whether operation during charging is supported. |  |

## 4.3 Cellular modem, SIM and U.S. readiness

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [ ] | Exact modem manufacturer/model and firmware revision? | Required before selecting U.S. SIM/carrier. |  |
| [ ] | LTE category / radio technologies / supported bands? | Validate against intended U.S. carrier(s). Do not depend on 3G availability. |  |
| [ ] | Antenna model and connection? | Record antenna part, gain, connector, placement and enclosure constraints. |  |
| [ ] | Which Canadian SIM/carrier is currently validated? | Capture APN and configuration used in successful bench tests. |  |
| [D] | Initial U.S. carrier/SIM? | Select one pilot carrier first; test a second option later if supplier diversity matters. |  |
| [ ] | What exactly is the "configure one file" SIM/APN change? | Identify file/path/fields and whether it can be changed remotely or without reflashing firmware. |  |
| [ ] | SIM form factor and serviceability? | Physical SIM vs eSIM; enclosure access; replacement process. |  |
| [T] | Network attach time and data usage per normal no-change wake? | Needed for battery and cellular plan sizing. |  |
| [T] | Network attach + content download under weak signal? | Test realistic low-RSSI condition, timeout, retry/backoff and battery impact. |  |
| [ ] | How does firmware obtain trusted time before HTTPS/TLS? | Network time / NTP / retained RTC strategy must survive deep sleep and cold boot. |  |
| [ ] | What happens if APN/carrier configuration is wrong after deployment? | Need a recoverable local/service path; avoid a configuration that can permanently strand the unit. |  |

## 4.4 NFC wake-on-tap and phone URL

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [ ] | Exact NFC device/tag/controller? Passive, dynamic tag, or MCU-controlled? | Document part and electrical wake path into ESP32. |  |
| [ ] | Can one phone tap both wake the display and open the URL reliably? | Demonstrate on iPhone and Android with screen fully asleep. |  |
| [D] | URL shape and device identification? | Recommend a DE Instore HTTPS URL containing a public device alias or signed short token - never a device secret. |  |
| [D] | Does tapping require the user to be signed in? | Recommend public landing can identify the display, but assignment/activation requires authenticated DE Instore access. |  |
| [ ] | Is NFC payload static or rewritable? | If rewritable, define who can rewrite it and how cloning/tampering is handled. |  |
| [ ] | What happens if cellular is unavailable after the tap? | Phone page should still identify the display and explain device connection state; device should retry safely. |  |
| [D] | QR fallback? | Recommend QR with same public display URL for phones/NFC failures and service diagnostics. |  |
| [ ] | Repeated taps while device is awake? | Define debounce/idempotent behavior so taps do not restart a render or corrupt state. |  |

## 4.5 E-Ink panel and rendering contract

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [ ] | Exact panel manufacturer/model, controller, interface and native resolution? | This defines the DE Instore render profile. |  |
| [ ] | Panel color set / bit depth / pixel encoding? | Document exact supported palette and byte/bit ordering. |  |
| [ ] | What image format does firmware currently accept? | Raw framebuffer, BMP, packed vendor format, etc. Capture exact specification and maximum payload size. |  |
| [D] | Where should image conversion happen? | Recommend DE Instore server prepares the panel-native asset; device validates and renders rather than doing general-purpose creative conversion. |  |
| [ ] | Full vs partial refresh support? | Document what is reliable, ghosting limits, and whether periodic full refresh is required. |  |
| [T] | Measured refresh time? | Cold boot to visible final image; download time measured separately from panel refresh. |  |
| [ ] | Busy/ready/failure indication from controller? | Need a deterministic way to claim render success rather than only "data was sent." |  |
| [ ] | Safe switched-power sequence? | Power-up, initialize, transfer, refresh complete, power-down - confirm panel retains image. |  |
| [ ] | Temperature operating range and refresh behavior? | E-Ink performance is temperature-sensitive; capture controller/panel limits and any compensation. |  |
| [ ] | What is retained after reset/brownout mid-refresh? | Previous known-good image should remain whenever possible; recovery state must be defined. |  |

# 5. Tay Review Checklist - Firmware, Reliability and Serviceability

## 5.1 Firmware state machine

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [D] | Canonical wake sequence | Recommend: wake -> identify reason -> health sample -> modem attach -> sync -> optional download -> render -> confirm -> schedule next wake -> sleep. |  |
| [ ] | Boot/wake reason reported? | NFC, RTC, power-on, watchdog, brownout, OTA, manual/service. |  |
| [ ] | Watchdog coverage? | Modem attach, HTTP/TLS, image download and E-Ink refresh must all have bounded timeouts. |  |
| [ ] | Retry/backoff strategy? | Avoid endless modem retries that consume the battery. Preserve last good image and sleep/retry later. |  |
| [ ] | Persistent state location? | Current content version/hash, last successful render, last error, next wake policy, provisioning data. |  |
| [ ] | Idempotency | Reboot/retry should never accidentally render an older asset or duplicate an activation event. |  |
| [ ] | Factory/service reset behavior? | Must not silently erase DE identity/credential unless an intentional service process does so. |  |
| [ ] | Local diagnostics interface? | Define serial/JTAG/service log method for bench troubleshooting. |  |

## 5.2 OTA firmware updates

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [D] | OTA supported? | Strongly recommended before field shipment. |  |
| [D] | Update packaging/signing | Firmware must be integrity-checked; production should use signed firmware and secure boot capabilities. |  |
| [D] | A/B or rollback strategy | Recommend previous known-good firmware remains bootable if update fails. |  |
| [ ] | Update trigger | Server advertises target firmware during sync; device downloads only when battery/network conditions are acceptable. |  |
| [ ] | Interrupted update behavior | Power/network loss must not brick unit. |  |
| [ ] | Version targeting | Need board revision + hardware profile so firmware can be assigned safely by cohort. |  |
| [ ] | Emergency rollback / disable rollout | DE Instore must be able to stop a rollout and return a pilot cohort to prior version. |  |

## 5.3 Security baseline

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [D] | Transport security | HTTPS/TLS for API, content and firmware. No plaintext production transport. |  |
| [D] | Device credential | Unique per-device credential. Do not share one secret across the fleet. |  |
| [D] | Credential storage | Use ESP32 secure-boot / flash-encryption capabilities or equivalent protected storage for production credentials. |  |
| [D] | Credential revocation/rotation | Server must disable a lost/stolen device and issue replacement credentials through a controlled service path. |  |
| [D] | Content integrity | Server provides SHA-256 (or equivalent) hash; device verifies complete asset before render. |  |
| [D] | OTA integrity | Signed firmware preferred; hash alone is not sufficient against a malicious source. |  |
| [D] | NFC security | NFC URL may identify the display but must not expose API secrets or permit unauthenticated content reassignment. |  |
| [ ] | Production debug policy | Decide whether JTAG/UART is disabled, locked, or physically/service protected on field units. |  |

## 5.4 Logging and remote diagnostics

| **State** | **Question / item** | **Recommended direction** | **Notes / Answer** |
| --- | --- | --- | --- |
| [D] | Minimum device event log | Boot reason, modem attach result, signal/carrier, sync result, content decision, download result, render result, battery, firmware, errors. |  |
| [D] | Ring buffer on device | Retain recent events through offline periods; upload compact records on next successful sync. |  |
| [ ] | Error taxonomy | Agree numeric/string error codes for modem, DNS/TLS, HTTP, content validation, panel, power and OTA failures. |  |
| [ ] | Last-seen timestamp | Server records every successful authenticated sync. |  |
| [ ] | Remote diagnostic request | Consider a server action asking next wake to include extended modem/GPS/log data. Keep optional to protect battery/data. |  |

# 6. Recommended DE Instore <-> Device Contract

Because the device spends most of its life asleep, DE Instore should use a pull/synchronize model rather than pretending we can always push. The server owns DESIRED STATE; the device reports OBSERVED STATE. The difference between those two states is what drives the UI and deployment status.

|  |
| --- |
| **Recommended optimization:** Use one compact authenticated sync transaction per wake. The device sends identity + current state + health; the server responds with whether anything changed, the next wake policy, and any content/firmware action. Avoid several API round trips when one will do, especially on cellular. |

## 6.1 Proposed normal wake sequence

| **Step** | **Device action** | **DE Instore effect / observable state** |
| --- | --- | --- |
| 1 | Wake from NFC, RTC or service/power reason. | Device may still be shown as sleeping/offline until authenticated check-in. |
| 2 | Read persistent identity, current content version/hash and basic battery state. | No network dependency yet. |
| 3 | Attach modem; obtain usable network/time. | If attach fails, retain last image, log failure, use bounded retry/backoff, then sleep. |
| 4 | POST one authenticated /device/v1/sync request. | Server updates last-seen, telemetry and reported content/firmware state. |
| 5 | Server returns no-change or desired content + optional firmware/config action. | Deployment moves from Available to Device Connected / Downloading as evidence arrives. |
| 6 | If content changed, download panel-ready asset over HTTPS; verify hash/size/profile. | Failed validation never replaces current content. |
| 7 | Power E-Ink subsystem; render; wait for controller-complete indication. | Only controller-complete (or equivalent verified signal) should become Rendered/Confirmed. |
| 8 | POST compact result event / confirmation. | Server records confirmed content version, timestamp, elapsed time and device health. |
| 9 | Persist state, set next wake, power down peripherals, deep sleep. | UI shows confirmed content and expected next check window. |

## 6.2 Example sync request - illustrative, not final

|  |
| --- |
| POST /device/v1/sync  Authorization: Bearer <per-device-token>  Content-Type: application/json  {  "device\_id": "DEI-EP-000123",  "hardware\_rev": "proto-1",  "firmware\_version": "0.9.3",  "wake\_reason": "nfc",  "current\_content": {  "content\_id": "asset-8f31",  "version": 17,  "sha256": "..."  },  "battery": {"voltage\_v": 11.7, "soc\_pct": 73},  "modem": {"imei": "...", "carrier": "...", "rssi\_dbm": -91},  "location": {"lat": 35.78, "lon": -78.64, "accuracy\_m": 35, "age\_s": 12},  "last\_result": {"state": "render\_confirmed", "error": null}  } |

Fields are examples. Tay should tell us which telemetry is inexpensive/reliable on his hardware. We should not force GPS acquisition or other expensive work merely to satisfy a schema; absent/unknown fields must be legal.

## 6.3 Example sync response - illustrative, not final

|  |
| --- |
| {  "server\_time": "2026-09-10T19:00:00Z",  "next\_wake\_seconds": 21600,  "desired\_content": {  "content\_id": "asset-b219",  "version": 18,  "url": "https://content.deinstore.tech/...signed...",  "bytes": 482331,  "sha256": "...",  "render\_profile": "dei-eink-32-p1",  "expires\_at": "2026-09-10T19:15:00Z"  },  "firmware": null,  "actions": []  } |

## 6.4 Result / confirmation event

After a content attempt, the device should post a concise event containing device ID, desired content version, outcome, render start/end times (or elapsed milliseconds), panel/controller result, battery after operation, and a bounded error code/message. The API must be idempotent so a retry does not create a second logical deployment.

# 7. Content and Render Profile Contract

DE Instore should keep the original creative asset and produce device-specific derivatives. A native panel profile should be explicit rather than inferred from screen size. This keeps content processing evolvable without reflashing devices and avoids putting image composition/cropping/business rules on an ESP32.

| **Profile field** | **What DE Instore needs from Tay** | **Why it matters** |
| --- | --- | --- |
| profile\_id | Stable name such as dei-eink-32-p1. | Server selects correct derivative and prevents cross-panel deployment. |
| pixel\_width / pixel\_height | Exact native addressable pixels and orientation. | No scaling ambiguity at device. |
| palette / bit depth | Exact colors or grayscale levels and encoding. | Server dithering/quantization must match panel. |
| payload format | Exact bytes/file accepted by firmware. | Defines derivative builder and validation. |
| max payload size | Hard/comfortable maximum. | Cellular/data/buffer sizing and server guardrail. |
| refresh modes | Full/partial, allowed regions, required full-refresh cadence. | Prevents ghosting and unsupported commands. |
| controller completion signal | BUSY pin/state/API or equivalent. | Basis for honest Rendered/Confirmed state. |
| temperature constraints | Panel/controller operating and refresh limits. | Avoid failed/poor refresh and protect hardware. |

# 8. Deep Sleep and Deployment Semantics

The UI must tell the truth about a sleeping cellular display. Unless the modem remains reachable or there is another remote wake mechanism, DE Instore cannot guarantee an immediate push to a device that is asleep. The platform should distinguish content being ready from the device actually checking in and rendering it.

| **DE Instore status** | **Meaning for native device** |
| --- | --- |
| Preparing | Server is validating/composing the panel-ready derivative. |
| Available to Device | Desired content is committed on the server; sleeping device has not yet checked in. |
| Device Connected | Device has authenticated during a wake cycle and received desired-state metadata. |
| Downloading | Device is retrieving the derivative. |
| Rendering | E-Ink controller is actively updating. |
| Confirmed | Device reports the target content version rendered successfully. |
| Deferred / Sleeping | Content is ready but next device wake is pending. |
| Failed / Attention | Authenticated attempt failed; expose error, last good content and next retry. |

|  |
| --- |
| **Product implication:** "Deploy Now" can be truly immediate when the device is already awake/connected or when a technician taps NFC to wake it. Otherwise the honest promise is "Available now - applies at next device wake." This is a feature of the low-power architecture, not an error condition. |

# 9. NFC Field Workflow - Recommended First-Party Experience

| **Step** | **Technician / user** | **Native display** | **DE Instore web app** |
| --- | --- | --- | --- |
| 1 | Tap phone to NFC target. | Wake interrupt fires. | Phone opens HTTPS display URL. |
| 2 | If needed, sign into DE Instore. | Begins modem attach / check-in. | URL resolves a public display alias to inventory record; secrets remain server-side. |
| 3 | Confirm screen/site context and choose content/product/campaign action. | May already be online and waiting for desired state. | Writes new desired content version and records operator/action. |
| 4 | Tap Activate / Deploy. | Next sync response contains desired content; device downloads and renders. | Shows live deployment phases using device evidence. |
| 5 | Observe physical update. | Posts Rendered/Confirmed or explicit failure. | Updates thumbnail/current content immediately when confirmed; retains audit event. |

This flow can replace unreliable camera scanning for screen identification. QR should remain as a service/fallback path, but NFC can become the preferred native-device handshake.

# 10. DE Instore Platform Data Needed for Each Native Display

| **Field group** | **Minimum fields** |
| --- | --- |
| Identity | device\_id, human label, hardware\_rev, panel\_profile, provider/device\_type=native, board serial if different. |
| Cellular | IMEI, SIM ICCID/eSIM identifier, configured carrier/APN profile, last carrier/radio tech, last RSSI/quality. |
| Firmware | current version, target version if staged, last OTA result, boot reason/reset reason. |
| Power | battery voltage, SoC estimate, charge state, low-battery flag, last sample time. |
| Content desired state | desired content\_id/version/hash, assignment/campaign source, requested\_at, requested\_by. |
| Content reported state | reported content\_id/version/hash, render\_confirmed\_at, last render duration/result. |
| Connectivity | last\_seen, last successful sync, next expected wake, consecutive failures, last error. |
| Location | assigned site/location plus optional device-reported GPS + age/accuracy. Assigned location remains authoritative for business context. |
| Field access | NFC/QR public alias or token mapping, not raw device credential. |

# 11. Acceptance Test Matrix - Before U.S. Field Pilot

| **Pass** | **Scenario** | **Acceptance evidence** | **Notes / Result** |
| --- | --- | --- | --- |
| [ ] | Normal NFC wake -> U.S. LTE attach -> DE sync -> new content -> confirmed | Complete end-to-end with timestamps and server/device logs. |  |
| [ ] | RTC/timer wake with no content change | Short transaction; no unnecessary panel power-up; correct next wake. |  |
| [ ] | RTC/timer wake with pending content | Updates without human tap. |  |
| [ ] | No cellular service | Retains prior image; bounded retries; useful error; sleeps; later recovery succeeds. |  |
| [ ] | Weak cellular signal | No brownout; bounded attach/download time; battery impact measured. |  |
| [ ] | Server unavailable / HTTP 5xx | No content loss; retry/backoff; later recovery. |  |
| [ ] | DNS/TLS/time failure | Explicit error category; no insecure fallback. |  |
| [ ] | Interrupted/corrupt content download | Hash fails; old image retained; no partial/corrupt render. |  |
| [ ] | Reset/brownout during render | Defined recovery; device never claims confirmed unless panel completed. |  |
| [ ] | Low battery at wake | Safe policy; avoids risky OTA/refresh as defined; reports condition. |  |
| [ ] | Repeated NFC taps | No reboot loop/duplicate activation/corrupt state. |  |
| [ ] | GPS unavailable / slow fix | Normal content workflow still functions if GPS is optional. |  |
| [ ] | Invalid/revoked device credential | Server denies; device reports auth failure; cannot fetch protected content. |  |
| [ ] | OTA success | Update, reboot, report new version, preserve content/state. |  |
| [ ] | OTA interrupted / bad image | Rollback or safe recovery demonstrated; device remains manageable. |  |
| [ ] | Wrong render profile assigned | Server/device guardrail rejects before harmful/meaningless panel write. |  |
| [ ] | 72-hour bench soak | Repeated wakes/updates; no memory leak, lockup, watchdog storm or state drift. |  |
| [ ] | Battery characterization run | Measured sleep current and representative update energy used to calculate realistic days/months for several wake/update schedules. |  |

# 12. PCB / Enclosure / Manufacturing Review Checklist

| **State** | **Review item** | **Notes / Owner / Decision** |
| --- | --- | --- |
| [ ] | Programming/debug pads or connector accessible during prototype and production test. |  |
| [ ] | Documented test points for battery rail, ESP32 rail, modem rail and E-Ink switched supply. |  |
| [ ] | Cellular antenna keep-out / ground / enclosure effects reviewed. |  |
| [ ] | GPS antenna placement works after final enclosure and display integration. |  |
| [ ] | NFC antenna/tap target remains reliable through final enclosure materials and mounting. |  |
| [ ] | SIM access/service method decided; accidental field ejection prevented. |  |
| [ ] | Battery pack is mechanically retained, protected and field-service strategy decided. |  |
| [ ] | Charging/power connector strain relief and polarity protection defined. |  |
| [ ] | Board/panel connectors keyed or otherwise protected against misconnection. |  |
| [ ] | Physical label includes DE device ID, human-short ID, QR fallback and required regulatory markings. |  |
| [ ] | End-of-line test can provision ID/credential, verify modem, NFC, panel, battery telemetry and one server check-in. |  |
| [ ] | Production credential injection process does not expose fleet-wide secrets in source code or a shared image. |  |
| [ ] | Hardware revision is machine-readable by firmware or provisioned reliably. |  |
| [ ] | Service procedure exists for modem/SIM/panel/PCB replacement and device reassociation. |  |

# 13. Information DE Instore Software Needs from Tay

The following package is enough for us to begin the native integration without waiting for the enclosure or production PCB. It does not need to be polished documentation; bench notes, header files, sample payloads and photos are sufficient initially.

| **Priority** | **Requested item** | **Why we need it** |
| --- | --- | --- |
| P0 | Exact ESP32-S3 module, flash/PSRAM, modem model, NFC part, E-Ink panel/controller, battery/BMS parts. | Defines constraints and avoids designing to assumptions. |
| P0 | Current successful bench wake sequence and a sample server request/response or source snippet showing "phone home." | Lets DE Instore align to the working implementation rather than replace it blindly. |
| P0 | Exact current image input format and render sequence. | Allows us to build the first server-side panel derivative. |
| P0 | U.S.-relevant LTE bands + Canadian carrier/SIM/APN currently used. | Needed immediately for U.S. pilot SIM selection. |
| P0 | NFC URL example and explanation of how the tap wakes the ESP32 and presents the URL to the phone. | Defines field activation flow and security boundary. |
| P1 | Current telemetry available: battery, RSSI, IMEI/ICCID, GPS, reset reason, firmware version. | Shapes sync payload and fleet health UI. |
| P1 | Current failure/retry behavior and watchdog behavior. | Lets us define honest deployment states and acceptance tests. |
| P1 | OTA status: implemented / planned / not yet designed. | May affect PCB/partition/provisioning decisions before shipment. |
| P1 | Measured sleep current and one representative full wake/update current or energy trace. | Turns "days/months" into a defensible operating model. |
| P2 | Prototype PCB/enclosure constraints and any decisions already locked. | Prevents software/security requirements from arriving too late. |

# 14. Open Decisions / Working Notes

Use this section during review. A decision is not final until owner and date are recorded. Add rows as needed.

| **#** | **Topic / question** | **Decision / answer** | **Owner** | **Date** |
| --- | --- | --- | --- | --- |
| 1 |  |  |  |  |
| 2 |  |  |  |  |
| 3 |  |  |  |  |
| 4 |  |  |  |  |
| 5 |  |  |  |  |
| 6 |  |  |  |  |
| 7 |  |  |  |  |
| 8 |  |  |  |  |
| 9 |  |  |  |  |
| 10 |  |  |  |  |
| 11 |  |  |  |  |
| 12 |  |  |  |  |

# 15. Proposed First Integration Milestone

Before we attempt campaign logic, scheduling UI, or production provisioning, prove one narrow vertical slice against Tay's bench unit or first U.S. unit:

* DE Instore creates a native display record with stable device ID and render profile.
* Device boots/wakes on a U.S. cellular connection and authenticates to a DE Instore test endpoint.
* Device sends firmware/battery/modem/current-content state in one sync request.
* DE Instore responds with a new panel-ready test asset, version and integrity hash.
* Device downloads, validates, renders and reports confirmed.
* DE Instore records desired vs reported content, last seen, confirmation timestamp and any error.
* Repeat with no content change; device should avoid an unnecessary E-Ink refresh.
* Repeat with a forced network failure; previous image must survive and later recovery must be observable.

|  |
| --- |
| **Exit criterion:** When this vertical slice is repeatable, we have the native provider contract. Everything above it - campaigns, products, field activation, scheduling and reporting - can then use the same DE Instore domain model without being tied to a specific modem or panel vendor. |

# Joint Review Signoff / Next Actions

| **Role** | **Name** | **Review complete** | **Next action / date** |
| --- | --- | --- | --- |
| Hardware / firmware | Tay | [ ] |  |
| DE Instore product / integration | Dean | [ ] |  |
| DE Instore team | Pete / Nick | [ ] |  |

*Document control: v0.1 is a requirements and questions draft derived from Tay's bench summary. Technical recommendations are proposals until jointly confirmed against the actual hardware, firmware and U.S. carrier test results.*