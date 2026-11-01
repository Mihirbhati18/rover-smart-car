"""Safe laptop-to-Arduino serial link for the Smart Car.

The current Arduino sketch accepts one-character commands.  This module keeps
that detail in one place so later camera, recovery, and voice code can all use
the same validated interface.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


_PROJECT_PACKAGES = Path(__file__).resolve().parent.parent / "serial-packages"
if _PROJECT_PACKAGES.is_dir():
    sys.path.insert(0, str(_PROJECT_PACKAGES))

try:
    import serial
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pySerial is missing. Install the packages listed in requirements.txt."
    ) from exc


COMMAND_BYTES = {
    "FORWARD": b"f",
    "BACKWARD": b"b",
    "LEFT": b"l",
    "RIGHT": b"r",
    "STOP": b"s",
    "LINE_TRACKING": b"1",
    "OBSTACLE_AVOIDANCE": b"2",
    "AI_START": b"A",
    "PRESENTATION": b"P",
    "AI_FORWARD": b"F",
    "AI_BACKWARD": b"B",
    "AI_LEFT": b"L",
    "AI_RIGHT": b"R",
    "AI_LEFT_TEST": b"J",
    "AI_RIGHT_TEST": b"K",
    "AI_STOP": b"S",
    "AI_RESUME": b"X",
    "AI_DISTANCE": b"D",
    "AI_SCAN": b"G",
    "EMERGENCY_STOP": b"!",
    "CLEAR_EMERGENCY": b"C",
    "STATUS": b"?",
}


@dataclass(frozen=True)
class ArduinoEvent:
    text: str
    received_at: float


class ArduinoSerial:
    """Validated serial command sender with background event reception."""

    def __init__(self, port: str = "COM3", baud_rate: int = 9600) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self._serial: serial.Serial | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._events: queue.Queue[ArduinoEvent] = queue.Queue()

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    def connect(self, startup_wait: float = 2.5) -> "ArduinoSerial":
        if self.is_open:
            return self

        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baud_rate,
            timeout=0.2,
            write_timeout=1.0,
        )
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="arduino-serial-reader",
            daemon=True,
        )
        self._reader_thread.start()

        # An Uno normally resets when a USB serial connection is opened.
        time.sleep(max(0.0, startup_wait))
        return self

    def disconnect(self, send_stop: bool = True) -> None:
        if send_stop and self.is_open:
            try:
                self.send("STOP")
                time.sleep(0.05)
            except (OSError, serial.SerialException):
                pass

        self._stop_event.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        if self._serial:
            self._serial.close()
        self._serial = None

    def send(self, command: str) -> None:
        normalized = command.strip().upper()
        if normalized not in COMMAND_BYTES:
            allowed = ", ".join(COMMAND_BYTES)
            raise ValueError(f"Unknown command {command!r}. Allowed: {allowed}")
        if not self.is_open or self._serial is None:
            raise RuntimeError("Arduino serial connection is not open.")

        self._serial.write(COMMAND_BYTES[normalized])
        self._serial.flush()

    def get_event(self, timeout: float | None = None) -> ArduinoEvent | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def send_route_pulse(self, direction: str, duration_ms: int) -> None:
        """Dedicated bounded route packet; legacy commands are unchanged."""
        code = {'FORWARD': 'F', 'LEFT': 'L', 'RIGHT': 'R'}.get(direction)
        if code is None or type(duration_ms) is not int or not 1 <= duration_ms <= 350:
            raise ValueError('Route pulse requires forward/left/right and 1–350 ms.')
        if not self.is_open or self._serial is None:
            raise RuntimeError('Arduino serial connection is not open.')
        self._serial.write(f'~{code}{duration_ms}\n'.encode('ascii'))
        self._serial.flush()

    def drain_events(self) -> list[ArduinoEvent]:
        events: list[ArduinoEvent] = []
        while True:
            event = self.get_event(timeout=0)
            if event is None:
                return events
            events.append(event)

    def probe_manual(self):
        if not self.is_open:
            raise RuntimeError('Arduino serial connection is not open.')
        self._serial.write(b'@\n')
        self._serial.flush()

    def send_manual_pulse(self, direction, duration_ms):
        code = {'FORWARD':'F', 'BACKWARD':'B', 'LEFT':'L', 'RIGHT':'R'}.get(direction)
        if code is None or type(duration_ms) is not int or not 1 <= duration_ms <= 350:
            raise ValueError('Manual pulse requires a direction and 1–350 ms.')
        if not self.is_open:
            raise RuntimeError('Arduino serial connection is not open.')
        self._serial.write(f'@{code}{duration_ms}\n'.encode('ascii'))
        self._serial.flush()

    def __enter__(self) -> "ArduinoSerial":
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.disconnect(send_stop=True)

    def _reader_loop(self) -> None:
        assert self._serial is not None
        while not self._stop_event.is_set() and self._serial.is_open:
            try:
                raw_line = self._serial.readline()
            except (OSError, serial.SerialException):
                return
            if not raw_line:
                continue
            text = raw_line.decode("utf-8", errors="replace").strip()
            if text:
                self._events.put(ArduinoEvent(text=text, received_at=time.time()))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely test the Arduino link.")
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument(
        "--command",
        default="STOP",
        choices=tuple(COMMAND_BYTES),
        help="STOP is the safe default",
    )
    parser.add_argument(
        "--allow-motion",
        action="store_true",
        help="Required before sending a movement or autonomous-mode command",
    )
    parser.add_argument(
        "--motion-seconds",
        type=float,
        default=0.5,
        help="Automatically STOP after this many seconds",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    safe_commands = {"STOP", "AI_START", "AI_STOP", "AI_DISTANCE", "AI_SCAN", "EMERGENCY_STOP", "STATUS"}
    dangerous_commands = set(COMMAND_BYTES) - safe_commands
    if args.command in dangerous_commands and not args.allow_motion:
        print("REFUSED: add --allow-motion to send anything except STOP.")
        return 2

    try:
        with ArduinoSerial(args.port, args.baud) as arduino:
            startup_events = arduino.drain_events()
            arduino.send(args.command)
            if args.command in dangerous_commands:
                time.sleep(max(0.0, args.motion_seconds))
                arduino.send("STOP")
            time.sleep(0.25)
            events = startup_events + arduino.drain_events()
    except serial.SerialException as exc:
        print(f"ARDUINO ERROR: {exc}")
        return 1

    print(f"ARDUINO OK: {args.port} at {args.baud} baud")
    print(f"Command sent: {args.command}")
    if events:
        for event in events:
            print(f"Arduino said: {event.text}")
    else:
        print("Arduino sent no text (this is normal for the current STOP command).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
