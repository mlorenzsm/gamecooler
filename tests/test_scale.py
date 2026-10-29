"""Checks the scale protocol (app/scale.py) and the simulator against each
other. Like test_i18n.py: no framework, exits non-zero on a problem.

    .venv/bin/python tests/test_scale.py

The encoder lives next to the decoder precisely so this test pins their
agreement — a change to one side that forgets the other fails here.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scale import HEADER, MODE_COUNT, ScaleReader, decode_frame, encode_frame  # noqa: E402

problems: list[str] = []


def check(name: str, ok: bool) -> None:
    if not ok:
        problems.append(name)
        print(f"FAIL {name}")


def roundtrip(weight: float, **kwargs):
    return decode_frame(encode_frame(weight, **kwargs))


# --- encoder/decoder round-trips ---------------------------------------------

r = roundtrip(2.5035)
check("weight survives the round-trip", r is not None and r.weight_kg == 2.5035)
check("stable by default", r.stable)
check("kg by default", r.unit == "kg")

r = roundtrip(0.0)
check("zero", r is not None and r.weight_kg == 0.0 and not r.negative)

r = roundtrip(-0.012)
check("negative weight (below tare)", r is not None and r.weight_kg == -0.012 and r.negative)

r = roundtrip(14.9995)
check("near capacity", r is not None and r.weight_kg == 14.9995)

r = roundtrip(1.5, stable=False)
check("unstable flag", r is not None and not r.stable)

r = roundtrip(1.5, overload=True)
check("overload flag", r is not None and r.overload)

# capacity overload (>15 kg) is the caller's business (the simulator passes
# overload=); the encoder itself only clamps when the 6 BCD digits overflow
r = roundtrip(200.0)
check("digit overflow clamps to overload", r is not None and r.overload)

r = roundtrip(123, mode=MODE_COUNT, decimals=0)
check("counting mode and 0 decimals", r is not None and r.mode == MODE_COUNT and r.weight_kg == 123)

r = roundtrip(2.5, unit="lb")
check("lb unit", r is not None and r.unit == "lb")

# --- decoder robustness -------------------------------------------------------

check("wrong header rejected", decode_frame(bytes([0x00, 0, 0, 0, 0, 0])) is None)
check("short frame rejected", decode_frame(bytes([HEADER, 0, 0])) is None)
check("non-BCD rejected", decode_frame(bytes([HEADER, 4, 0xAB, 0, 0, 0])) is None)

# --- reader against the simulator over real TCP --------------------------------

PORT = 18765
sim = subprocess.Popen(
    [sys.executable, str(ROOT / "tools" / "scale_sim.py"), "--port", str(PORT), "--weight", "2.5"],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
)
try:
    reader = ScaleReader(f"socket://127.0.0.1:{PORT}", 9600)
    reader.start()
    reading = None
    for _ in range(50):
        reading = reader.latest()
        if reading is not None and reading.stable:
            break
        time.sleep(0.1)
    check("reader gets a stable value from the simulator",
          reading is not None and reading.stable and abs(reading.weight_kg - 2.5) < 0.001)

    # a weight change wobbles first, then settles on the new value
    sim.stdin.write("1.2\n")
    sim.stdin.flush()
    settled = None
    for _ in range(80):
        r = reader.latest()
        if r is not None and r.stable and abs(r.weight_kg - 1.2) < 0.001:
            settled = r
            break
        time.sleep(0.1)
    check("reader settles on the new weight", settled is not None)

    # simulator off -> reading goes stale -> latest() is None
    sim.stdin.write("off\n")
    sim.stdin.flush()
    time.sleep(ScaleReader.STALE_S + 0.5)
    check("stale stream reads as no scale", reader.latest() is None)
finally:
    sim.kill()

# --- resync on garbage ---------------------------------------------------------

rdr = ScaleReader("socket://unused:1", 9600)
buf = bytearray(b"\x12\x34" + encode_frame(3.25) + b"\xff\x07" + encode_frame(4.5))
rdr._drain(buf)
check("resyncs over garbage and false headers",
      rdr._latest is not None and rdr._latest[0].weight_kg == 4.5)

print(f"{len(problems)} problems")
sys.exit(1 if problems else 0)
