"""Simulated bench scale, until the real SBS-TW-15C arrives.

Behaves like the scale behind the Elfin-EW10 WLAN bridge: a TCP server that
streams the 6-byte frames (app/scale.py) at ~10 Hz to every client. Point
the app at it in config.yaml:

    scale:
      port: socket://127.0.0.1:8765

Run it:

    .venv/bin/python tools/scale_sim.py            # interactive
    .venv/bin/python tools/scale_sim.py --port 8765

Interactive commands (stdin):

    2.5       put 2.5 kg on the platform (settles after a short wobble)
    0         take everything off
    tare      same as 0
    hold      send unstable frames until further notice (a hand on the pan)
    ok        stable again
    off       stop sending (scale switched off); "on" resumes
    q         quit

Weight changes wobble first — a few unstable frames drifting toward the
target — exactly what the app's "wait for stable" logic must survive.
"""

import argparse
import random
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scale import encode_frame  # noqa: E402


class SimState:
    def __init__(self):
        self.lock = threading.Lock()
        self.target = 0.0        # what lies on the platform
        self.current = 0.0       # what the load cell reports right now
        self.hold = False        # True: never report stable
        self.powered = True

    def set_weight(self, kg: float) -> None:
        with self.lock:
            self.target = kg

    def frame(self) -> bytes | None:
        """The next frame, ~1/10 s of scale behaviour, or None when off."""
        with self.lock:
            if not self.powered:
                return None
            # drift toward the target like a settling load cell
            diff = self.target - self.current
            if abs(diff) > 0.0005:
                self.current += diff * 0.4 + random.uniform(-0.002, 0.002)
                stable = False
            else:
                self.current = self.target
                stable = not self.hold
            if self.hold:
                self.current = self.target + random.uniform(-0.01, 0.01)
            return encode_frame(round(self.current, 4), stable=stable,
                                overload=self.current > 15.0)


def serve(state: SimState, host: str, port: int) -> None:
    clients: list[socket.socket] = []
    lock = threading.Lock()

    def accept_loop(server: socket.socket) -> None:
        while True:
            conn, addr = server.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print(f"  [client connected: {addr[0]}:{addr[1]}]")
            with lock:
                clients.append(conn)

    server = socket.create_server((host, port), reuse_port=True)
    threading.Thread(target=accept_loop, args=(server,), daemon=True).start()
    print(f"scale simulator on {host}:{port} — app config: scale: {{port: socket://{host}:{port}}}")

    def stream() -> None:
        while True:
            frame = state.frame()
            if frame is not None:
                with lock:
                    for conn in clients[:]:
                        try:
                            conn.sendall(frame)
                        except OSError:
                            clients.remove(conn)
                            conn.close()
                            print("  [client gone]")
            time.sleep(0.1)                      # ~10 frames/s, like the scale

    threading.Thread(target=stream, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--weight", type=float, default=0.0, help="initial weight in kg")
    args = parser.parse_args()

    state = SimState()
    state.target = state.current = args.weight
    serve(state, args.host, args.port)

    print("commands: <kg> | tare | hold | ok | off | on | q")
    run_commands(state)
    # Without a terminal (background, nohup, systemd) stdin is empty or
    # closed: keep streaming anyway instead of exiting with the read loop.
    if not sys.stdin.isatty():
        print("stdin closed — streaming on (kill the process to stop)")
        while True:
            time.sleep(3600)


def run_commands(state: SimState) -> None:
    for line in sys.stdin:
        cmd = line.strip().lower().replace(",", ".")
        if cmd in ("q", "quit", "exit"):
            return
        elif cmd in ("tare", "0"):
            state.set_weight(0.0)
            print("  platform empty")
        elif cmd == "hold":
            state.hold = True
            print("  unstable (hand on the pan)")
        elif cmd == "ok":
            state.hold = False
            print("  stable again")
        elif cmd == "off":
            state.powered = False
            print("  scale off — 'on' resumes")
        elif cmd == "on":
            state.powered = True
            print("  scale on")
        elif cmd:
            try:
                state.set_weight(float(cmd))
                print(f"  {float(cmd):.3f} kg on the platform (settling …)")
            except ValueError:
                print("  ? commands: <kg> | tare | hold | ok | off | on | q")


if __name__ == "__main__":
    main()
