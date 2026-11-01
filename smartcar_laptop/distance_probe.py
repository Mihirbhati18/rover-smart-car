"""Stationary ultrasonic readout for the AI-recovery sketch; no movement."""

from __future__ import annotations

import argparse
import time

from arduino_serial import ArduinoSerial


def parse_distance_event(text: str) -> int | None:
    prefix = "AI:DIST_CM="
    if not text.startswith(prefix):
        return None
    try:
        value = int(text[len(prefix):])
    except ValueError:
        return None
    return value if 3 <= value <= 300 else None


def wait_for_prefix(arduino: ArduinoSerial, prefix: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = arduino.get_event(timeout=0.2)
        if event is None:
            continue
        print(f"Arduino: {event.text}")
        if event.text.startswith(prefix):
            return event.text
    raise TimeoutError(f"Arduino did not report {prefix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read distance while the car is stopped")
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.count <= 20:
        parser.error("--count must be between 1 and 20")

    with ArduinoSerial(args.port) as arduino:
        wait_for_prefix(arduino, "CAPS:AI_RECOVERY_V2_DISTANCE")
        arduino.send("AI_START")
        wait_for_prefix(arduino, "AI:READY:")
        try:
            time.sleep(0.4)  # Sensor servo has centred before the first reading.
            readings: list[int] = []
            for _ in range(args.count):
                arduino.send("AI_DISTANCE")
                response = wait_for_prefix(arduino, "AI:DIST", timeout=1.0)
                value = parse_distance_event(response)
                if value is None:
                    print("Distance invalid; do not assume the path is clear.")
                else:
                    readings.append(value)
                    print(f"Front distance: {value} cm")
                time.sleep(0.15)
            if readings:
                print(f"Valid readings: {len(readings)}/{args.count}; range {min(readings)}–{max(readings)} cm")
            else:
                print("No valid ultrasonic readings.")
                return 1
        finally:
            arduino.send("AI_STOP")
            arduino.send("AI_RESUME")
    print("No movement commands were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
