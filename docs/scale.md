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

> **Still to be confirmed:** Metro blocks fetching the product page directly
> (HTTP 403). The model identification comes from search results, not from the
> page itself. Before buying, check the product description, especially whether
> the **RS-232 data cable is included** or has to be ordered separately
> (some retailers charge ~€20 extra for it).

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
for this use case. Double-check exact model numbers and prices when buying; the
product range changes.

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

The Mac has no serial port, so a **USB→RS-232 adapter** is needed as well.
Important: get one with an **FTDI chipset** — in our experience CH340/PL2303
cause more driver trouble on macOS.

The scale then shows up as `/dev/tty.usbserial-*` and can be accessed with
`pyserial`. (USB scales with a *virtual* serial port (CDC-ACM) behave
identically but show up as `/dev/tty.usbmodem*`.)

## Open question: can the scale be polled?

**This is the critical point and must be settled before buying.**

RS-232 scales typically support several transmission modes:

- **manual** — only on key press
- **automatic when stable** — sends on every stable weighing
- **continuous** — constant stream
- **on request** — the software sends a command, the scale replies

The integration needs **"on request"**. Some scales can only send
continuously or only on key press — then a "weigh row" button in the UI
wouldn't be possible; instead you'd have to physically press the key 12 times
for 12 rows.

The SBS-TW series manual has a section on RS-232 transmission, but the exact
**command syntax** couldn't be extracted. Before implementing, read the
included manual and check the mode (typically: 9600 baud, 8N1).

If the scale **can't** be polled, the ESP32 variant (below) is the better
choice.

## Integration sketch

Fits into the existing FastAPI app on the Mac, which already drives the
printer — no second service needed:

- `pyserial` opens `/dev/tty.usbserial-*`
- reads in a separate thread (serial ports block)
- endpoint e.g. `GET /scale/weight` → `{"ok": true, "weight_kg": 2.503}`
- the frontend polls this endpoint; a "weigh row" button fills in the weight field
- scale configurable in `config.yaml` (port, baud rate, model)

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
- Steinberg Systems: https://www.steinbergsystems.de
- `serial-scale-bench`: https://pypi.org/project/serial-scale-bench/
- `serial-scale-hx711`: https://pypi.org/project/serial-scale-hx711/
