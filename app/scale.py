"""Bench scale over RS-232 (docs/scale.md).

The scale streams 6-byte binary frames continuously; there is no poll
command. A background thread drains the stream and keeps the latest frame,
GET /scale/weight (main.py) returns it.

The frame format follows the SBS-TW-15C manual. Encoder and decoder live
side by side here so the simulator (tools/scale_sim.py) and the tests can
never drift from what the app expects — and when the real hardware shows a
deviation (the unit byte's values are an assumption, see UNITS), there is
exactly one place to fix.

The port is a pyserial URL: "/dev/tty.usbserial-..." for a USB adapter,
"socket://<ip>:<port>" for the WLAN bridge (Elfin-EW10) — and for the
simulator, which speaks the same TCP.
"""

import threading
import time
from dataclasses import dataclass

import serial

FRAME_LEN = 6
HEADER = 0xFF

# Status byte: bits 0-2 decimal point position, 3-4 mode, 5 negative,
# 6 stable, 7 overload. The manual writes "D7 - 2 (Übergewicht)", which is
# no bit value; read as "bit 7 set" until the hardware says otherwise.
MODE_WEIGH, MODE_COUNT, MODE_PERCENT = 0, 1, 2

# Unit byte values: an assumption — the manual only names the units.
# To be confirmed against the real scale; the simulator uses the same table.
UNITS = {0x00: "kg", 0x01: "lb"}


@dataclass
class Reading:
    weight_kg: float
    stable: bool
    negative: bool
    overload: bool
    mode: int
    unit: str


def encode_frame(weight: float, *, decimals: int = 4, stable: bool = True,
                 mode: int = MODE_WEIGH, overload: bool = False,
                 unit: str = "kg") -> bytes:
    """A frame as the scale would send it. Used by the simulator and tests."""
    digits = round(abs(weight) * 10 ** decimals)
    if digits > 999999:
        digits, overload = 999999, True
    text = f"{digits:06d}"
    status = (
        (decimals & 0b111)
        | (mode & 0b11) << 3
        | (weight < 0) << 5
        | stable << 6
        | overload << 7
    )
    # BCD1 holds the two least significant digits, BCD3 the two most.
    bcd = [int(text[i]) << 4 | int(text[i + 1]) for i in (4, 2, 0)]
    unit_byte = next((b for b, name in UNITS.items() if name == unit), 0x00)
    return bytes([HEADER, status, *bcd, unit_byte])


def decode_frame(frame: bytes) -> Reading | None:
    """One 6-byte frame -> Reading, or None if it can't be a frame (resync)."""
    if len(frame) != FRAME_LEN or frame[0] != HEADER:
        return None
    status = frame[1]
    digits = []
    for b in frame[4:1:-1]:                      # BCD3, BCD2, BCD1
        hi, lo = b >> 4, b & 0xF
        if hi > 9 or lo > 9:
            return None                          # not BCD: mid-stream garbage
        digits += [hi, lo]
    value = int("".join(map(str, digits))) / 10 ** (status & 0b111)
    negative = bool(status & 1 << 5)
    return Reading(
        weight_kg=-value if negative else value,
        stable=bool(status & 1 << 6),
        negative=negative,
        overload=bool(status & 1 << 7),
        mode=status >> 3 & 0b11,
        unit=UNITS.get(frame[5], "kg"),
    )


class ScaleReader:
    """Drains the scale's stream in a daemon thread, keeps the latest frame.

    Reconnects on every error, so the scale (or the WLAN bridge) can be
    switched off and on without restarting the app. A reading older than
    STALE_S counts as "no reading": the stream runs at ~10 frames/s, so
    silence means the connection is gone, not that nothing changed.
    """

    STALE_S = 2.0

    def __init__(self, url: str, baudrate: int):
        self.url = url
        self.baudrate = baudrate
        self._lock = threading.Lock()
        self._latest: tuple[Reading, float] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="scale-reader")
            self._thread.start()

    def latest(self) -> Reading | None:
        with self._lock:
            if self._latest and time.monotonic() - self._latest[1] <= self.STALE_S:
                return self._latest[0]
        return None

    def _run(self) -> None:
        buf = bytearray()
        while True:
            try:
                with serial.serial_for_url(self.url, baudrate=self.baudrate, timeout=1) as port:
                    buf.clear()
                    while True:
                        data = port.read(64)
                        if data:
                            buf += data
                            self._drain(buf)
            except Exception:
                time.sleep(2)

    def _drain(self, buf: bytearray) -> None:
        """Parse every complete frame in the buffer, resyncing on garbage."""
        while True:
            start = buf.find(HEADER)
            if start < 0:
                buf.clear()
                return
            del buf[:start]
            if len(buf) < FRAME_LEN:
                return
            reading = decode_frame(bytes(buf[:FRAME_LEN]))
            if reading is None:
                del buf[:1]                      # false header: skip, resync
                continue
            del buf[:FRAME_LEN]
            with self._lock:
                self._latest = (reading, time.monotonic())
