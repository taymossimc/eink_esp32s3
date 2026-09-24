# Fleet campaign sync validation

Validated against device `tmads-eink-a4cb8fdd9120` and the production Digital
Engine deployment on Arachne on 2026-09-22.

## Confirmed behavior

- All 8 campaigns have READY media, unique UPC mappings, normally published
  contracts, spend grants, and device-group targeting.
- The signed preload catalog contains all 8 campaigns. Firmware verifies the
  pinned Ed25519 signer and verifies every rendered asset by SHA-256 and byte
  length before committing it.
- USB held both portrait and landscape variants: 16 verified files, 3,405,961
  bytes total. A subsequent 0° to 90° switch downloaded 0 bytes.
- Cloud SVG and first-page PDF conversion produced valid WebP derivatives while
  retaining original source files.
- Native ESP32 WebP and SVG decoders compile into firmware, and offline browser
  conversion falls back to them when browser conversion is unavailable.
- Server-side barcode decoding normalized EAN-13 to UPC-A, resolved UPC
  `990000000004`, deployed its latest valid contract, and returned the
  delivery/sync/display acknowledgement shape.
- Fleet telemetry returned firmware, schedule, orientation, active asset, GNSS,
  connectivity, USB capacity/free space, campaign-cache bytes, display errors,
  and the INA219 sensor error instead of silently hiding battery state.

## Live orientation and campaign matrix

Four distinct promoted campaigns were displayed at 90°, 180°, 270°, and 0°.
Each report confirmed the expected campaign asset hash and applied orientation.

- End-to-end command-to-confirmation: median 455.40 s; range 405.35–495.43 s.
- Device sync cycle: median 203.174 s; range 202.897–203.647 s.
- Manifest fetch: median 1.629 s; range 1.619–1.666 s.
- Catalog fetch: median 1.624 s; range 1.613–1.632 s.
- Cache reconciliation: median 0.449 s; range 0.301–0.596 s.
- JPEG decode/packing: median 3.888 s; range 3.764–4.040 s.
- SPI transfer across both controllers: median 7.621 s; range 7.612–7.628 s.
- BUSY refresh/recovery: 86.870 s in all four recovery-path cases.
- Sync acknowledgement: median 1.587 s; range 1.501–1.618 s.
- Stable cached poll with no refresh: approximately 5.2–5.6 s, with zero
  downloads and zero display transfer.
- Final dual-variant cache reuse test: 130.251 s device cycle, 195.16 s until
  server-observed confirmation, zero downloads, all 16 variants verified.

The panel intermittently failed to assert BUSY after DRF. Firmware 1.6.10 added
a single full 5 V rail discharge/retry; all four orientation cases then
completed. Firmware 1.6.11 retained both orientation variants to eliminate
downloads when orientation changes.

## Build and service checks

- PlatformIO firmware build succeeded at 31.4% RAM and 29.5% flash.
- Edge canonical signed-response self-test verified the pinned signer and exact
  RFC 8785 payload bytes.
- Control-plane SVG/PDF conversion test passed.
- Shelf decode, lookup, deployment, and acknowledgement tests passed.
- Control plane, edge distributor, and public gateway were healthy after
  rebuild.

## Hardware-dependent scenarios

WiFi plus USB-present paths were exercised live. Cellular-only, physically
removed USB, and phone-connected captive-AP upload require changing physical
connectivity and cannot be safely simulated while the installed modem, USB
drive, and saved WiFi remain connected. Their fallback code paths compile and
are exposed in the portal, but those three physical permutations still require
an on-site pass.
