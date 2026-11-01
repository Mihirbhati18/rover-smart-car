"""Read left/centre/right ultrasonic distances while AI recovery is stopped."""

from __future__ import annotations

import argparse
import re
import time

from arduino_serial import ArduinoSerial
from distance_probe import wait_for_prefix


SCAN_PATTERN = re.compile(r"^AI:SCAN:L=(-?\d+),C=(-?\d+),R=(-?\d+)$")


def parse_scan_event(text: str) -> dict[str, int | None] | None:
    match = SCAN_PATTERN.fullmatch(text)
    if match is None:
        return None
    values = [int(value) for value in match.groups()]
    return {
        direction: value if 3 <= value <= 300 else None
        for direction, value in zip(("left", "centre", "right"), values)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Stationary ultrasonic side scan")
    parser.add_argument("--port", default="COM3")
    args = parser.parse_args()

    with ArduinoSerial(args.port) as arduino:
        wait_for_prefix(arduino, "CAPS:AI_SIDE_SCAN_V1")
        arduino.send("AI_START")
        wait_for_prefix(arduino, "AI:READY:")
        try:
            time.sleep(0.4)
            arduino.send("AI_SCAN")
            response = wait_for_prefix(arduino, "AI:SCAN:", timeout=3.0)
            values = parse_scan_event(response)
            if values is None:
                print(f"Invalid scan response: {response}")
                return 1
            for direction, distance in values.items():
                print(f"{direction.title()}: {distance if distance is not None else 'invalid'} cm")
            if any(distance is None for distance in values.values()):
                print("At least one reading is invalid; do not choose a turn from this scan.")
                return 1
            print("Readings only. No turn or movement command was sent.")
            return 0
        finally:
            arduino.send("AI_STOP")
            arduino.send("AI_RESUME")


if __name__ == "__main__":
    raise SystemExit(main())
