# Flux PCB continuation (after usage plan reset)

## User layout changes (2026-09-24)
- J1/J1B ESP32 sockets moved between Pi mounting holes (H1–H4) — good placement
- J8 40-pin Pi male header on BOTTOM, pin 1 lower-right from top view
- Mounting holes H1–H4 at 58×49 mm Pi HAT spacing

## Problem (from user + screenshot)
- Many **airwires/ratsnest** — footprints placed but **not routed**
- Peripheral cluster (J2 UPS, J4 NFC, J5 e-paper, J6 modem, J7 battery, Q1/Q3 switch) still **off board edge**
- **J8 not on any net** in schematic — no 5V/3V3/GND to Pi header
- Schematic nets exist for J1/J1B/J2–J7 but PCB traces not drawn

## Work order for Flux agent

### 1. Bring components on-board
Move inside 100×100 mm (or enlarge board to ~130×65 mm HAT+ carrier):
- J2 (UPS power), J4 (NFC), J5 (e-paper), J6 (modem), J7 (UPS I2C)
- Q1, Q3, R2/R4/R6, C3/C4 (5.3V switch path)

### 2. Power connectivity (priority)
| Net | Source | Destinations |
|-----|--------|--------------|
| UPS_5V | J2 pin 1 (5.3V measured) | J1 pin 21, Q1 source |
| UPS_3V3 | J2 pin 2 (~3.3V) | J4 pin 7 (NFC VCC) |
| PERIPH_5V_SW | Q1 drain | J5 pin 2, J6 pin 1 |
| GND | J2 pin 3 | All connectors, J1 pin 22, J8 Pi GND pins |

### 3. J8 Raspberry Pi header (BOTTOM)
Minimum power service (mechanical Pi stack):
- Pi pin 2/4 → UPS_5V or PERIPH_5V_SW (document which — likely UPS_5V always-on vs switched)
- Pi pin 1/17 → UPS_3V3 (3.3V)
- Pi pin 6,9,14,20,25,30,34,39 → GND (standard Pi grounds)
- **Do NOT** auto-map ESP32 GPIO to Pi GPIO unless user requests — power + GND only for now

### 4. Signal routing from schematic
Route per locked prototype netlist:
- J1/J1B ↔ J4 (NFC), J5 (EPD SPI), J6 (modem UART), J7 (battery I2C)
- GPIO4 PERIPH_PWR_EN → Q3 gate path

### 5. Verify
- ERC/DRC clean or documented waivers
- J1/J1B still 25.40 mm spacing
- Report final coordinates

## Electrical constants
- UPS output: **5.3 V** (not 5.0 V)
- Modem UART: **3.3 V** logic
- Single switched 5V rail (display + modem), NFC on always-on 3V3

## Flux thread
https://www.flux.ai/taymoss/esp32-s3-e-paper-nfc-controller~px?thread=20079680-4adf-4139-9909-cd0c019d1867
