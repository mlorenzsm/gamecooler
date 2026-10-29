# Scale integration (planned)

Status: **Idea / not implemented yet.** A scale is to be bought later and then
integrated. This document records the research so the decision doesn't have to
be made again.

Goal: take the weight directly from the scale when filling in a bulk print row
or doing a single print, instead of typing it in.

## Model

The preferred model is the **Steinberg Systems SBS-TW-15** (Metro Marketplace
link, see below) — a bench scale, **15 kg / 0.5 g**, with an **RS-232**
interface.

> **Confirmed (expondo product page, Oct 2026):** the model sold as
> "Tischwaage 15 kg / 0,5 g" is the **SBS-TW-15C** (the C variant — see note
> below), with RS-232. The **data cable is NOT included**: the Lieferumfang
> lists only scale, 6V battery and power supply. Order a cable (and the
> USB→RS-232 FTDI adapter) separately.
>
> Cheapest source was expondo directly (~€52), Metro ~€58, Amazon ~€79,
> Otto ~€95. expondo is the manufacturer, so warranty handling is simplest
> there.

Steinberg Systems is expondo's house brand (`steinbergsystems.de`), not to be
confused with Steinberg (audio software, `steinberg.net`) — search results mix
up the two brands heavily.

### SBS-TW series

| Model | Capacity | Readability | Interface |
|---|---|---|---|
| SBS-TW-6C | 6 kg | finer | RS-232 |
| **SBS-TW-15** | **15 kg** | **0.5 g** | **RS-232** |
| SBS-TW-30C | 30 kg | 1 g | RS-232 |

The suffix **C** marks the counting-scale variant (piece counting) — irrelevant
for this use case, but harmless: what expondo actually sells at 15 kg / 0.5 g
**is the SBS-TW-15C**, and the counting feature simply goes unused. Double-check
exact model numbers and prices when buying; the product range changes.

## Why a cheap one is good enough

The precision is massively oversized for this purpose, so the cheap variant is
fine:

At €16/kg, an error of 0.5 g on a 2.5 kg haunch amounts to
**0.8 cents**. Even a 50 g error would be under €1. The SBS-TW-15's
readability (0.5 g at 15 kg) is roughly **30,000× finer** than price
calculation requires.

The limiting factor isn't readability anyway, but the load cell's linearity
and creep — realistically a few grams.

15 kg is enough for individual cuts (a 2.5 kg haunch fits comfortably). It is
**not** enough for a whole roe deer — that would need a larger platform scale.

## Required hardware

Two ways to get the RS-232 signal to the Mac:

### A: USB→RS-232 adapter (wired)

The Mac has no serial port, so a **USB→RS-232 adapter** is needed.
Important: get one with an **FTDI chipset** — in our experience CH340/PL2303
cause more driver trouble on macOS.

The scale then shows up as `/dev/tty.usbserial-*` and can be accessed with
`pyserial`. (USB scales with a *virtual* serial port (CDC-ACM) behave
identically but show up as `/dev/tty.usbmodem*`.)

### B: WLAN bridge (serial device server) — preferred

A serial device server exposes the RS-232 port as a TCP socket over Wi-Fi:
no USB adapter, no driver, and the scale doesn't have to sit near the Mac.
A transparent TCP bridge forwards the scale's binary stream untouched; the
app opens `socket://<ip>:<port>` instead of `/dev/tty.*` — same parser.

- **Hi-Flying Elfin-EW10** (~€20–37): tiny, plugs onto the DB9, needs 5V.
- **USR-W610** (~€40–60): industrial, extra Ethernet port, watchdog.

**Powering the EW10 from the scale itself:** the EW10 accepts **5–18 V DC**
on pin 7/8 of its RJ45 connector (manual: 5–36 V module input, ~200 mA,
<0.7 W). The scale runs on a **6 V/4 Ah** lead-acid battery (charged by an
8 V/0.5 A supply) — squarely inside that range, so the EW10 can be wired
directly to the battery terminals and the whole thing becomes one
self-contained wireless scale with no second wall wart. Caveats:

- **Runtime:** ~0.7 W ≈ 115 mA at 6 V drains the 4 Ah battery in ~35 h by
  itself; combined runtime drops from ~100 h to a day or two. Fine when the
  scale usually sits plugged in.
- Wire the EW10 **behind the scale's power switch** (if the switch is in
  the battery path), or it drains the battery while the scale is "off".
- Opening the scale and tapping the battery leads voids the warranty —
  the zero-modification alternative is the EW10 with its own small USB
  supply, velcroed to the scale.
- Check DTE/DCE pinout of the scale's port; a null-modem adapter between
  scale and bridge may be needed (cheap, but annoying when missing).

## Transmission mode: continuous stream (settled, Oct 2026)

**Settled from the manual's RS-232 section.** The spec describes only an
output format and no request command: the scale **streams frames on its
own** (continuous mode). That is not the "on request" mode the first draft
of this document hoped for — but it is just as good, arguably simpler:

- a background thread reads the stream and keeps the **latest frame**
- `GET /scale/weight` returns that value instantly — no command round-trip
- the frame carries a **stable flag**, so the "weigh row" button can wait
  for a stable reading instead of grabbing a mid-wobble value

### Frame format (6 bytes, binary)

"10-Bit UART signal" = 1 start + 8 data + 1 stop, i.e. plain **8N1**.
The baud rate is not in this section of the manual — check the neighbouring
pages when the scale arrives (9600 is the usual default for this series).

| Byte | Content |
|---|---|
| 1 | `0xFF` — frame header (sync marker) |
| 2 | status: bits 0–2 decimal point position, 3–4 mode (`00` weighing, `01` counting, `10` percent), 5 negative, 6 **stable**, 7 overload |
| 3 | BCD1 — weight digits, LSB |
| 4 | BCD2 |
| 5 | BCD3 — HSB |
| 6 | unit: kg or lb |

Parsing: resync on `0xFF`, read 5 more bytes, decode the 6 BCD digits,
place the decimal point per status bits 0–2. Accept only frames with
mode = weighing, stable = 1, overload = 0; honour the sign bit.

Example: 2.5035 kg → digits `025035`, decimal position 4.

## Integration sketch

Fits into the existing FastAPI app on the Mac, which already drives the
printer — no second service needed:

- `pyserial` opens `/dev/tty.usbserial-*` — or, with the WLAN bridge below,
  a plain TCP socket (`pyserial` handles both via `serial.serial_for_url`,
  `socket://<ip>:<port>` for the bridge)
- a separate thread reads the continuous stream and keeps the latest frame
  (serial ports block; the stream must be drained either way)
- endpoint e.g. `GET /scale/weight` → `{"ok": true, "weight_kg": 2.503, "stable": true}`
- the frontend polls this endpoint; a "weigh row" button takes the next
  stable value and fills in the weight field
- scale configurable in `config.yaml` (serial port or host:port, baud rate)

The weight is **nullable** — the integration is a convenience, not a
prerequisite. The app must be fully functional without a scale.

### Existing libraries

- `serial-scale-bench` — driver for Kern/Mettler-Toledo, auto-detects two
  ASCII protocols and even ships a FastAPI server
  (`GET /weight`, `POST /tare`, `GET /status`)
- `serial-scale-hx711` (BSD-3) — for the ESP32 variant

## Alternatives (if the SBS-TW-15 doesn't fit)

| Option | Price | Capacity | Interface |
|---|---|---|---|
| **ESP32 + HX711 + load cell** | **~€50–80** | 20 kg, ~5–10 g | **HTTP/Wi-Fi** |
| Keyboard-wedge scale | ~€20–40 | varies | USB HID |
| AE ADAM CPWplus | ~€229 | platform | RS-232 |
| KERN VB15K2DM | ~€300 | 15 kg / 5 g | RS-232 |

**ESP32 + HX711** is not only cheaper but also **architecturally the cleanest
solution**: it delivers the weight directly over HTTP/MQTT, so no `/dev/tty`,
no `pyserial`, no USB adapter, no driver. The trade-off is a DIY project
(flash firmware, calibrate, build a platform).

**Keyboard wedge** (e.g. A&D "Quick USB") needs zero code but is push-only —
you can't *poll* a value, only wait for a key press. On top of that there's a
macOS problem: the scale identifies itself as a numeric keypad with NumLock,
which macOS doesn't support, so values can arrive garbled. Unsuitable for a
12-row workflow.

## Sources

- Metro Marketplace (SBS-TW-15):
  https://www.metro.de/marktplatz/product/78da41e1-e581-47fb-96a5-37cf35293abd
- expondo (manufacturer, cheapest):
  https://www.expondo.de/steinberg-systems-tischwaage-15-kg-0-5-g-21-x-27-cm-lcd-10030492
- Steinberg Systems: https://www.steinbergsystems.de
- Elfin-EW10 (Hi-Flying): http://www.hi-flying.com/elfin-ew10-elfin-ew11
  (manual: Elfin-EW1X User Manual, power/pin specs on pp. 7–9)
- USR-W610: https://shop.usriot.com/rs232/rs485-to-wifi-converters-usr-w610
- `serial-scale-bench`: https://pypi.org/project/serial-scale-bench/
- `serial-scale-hx711`: https://pypi.org/project/serial-scale-hx711/
