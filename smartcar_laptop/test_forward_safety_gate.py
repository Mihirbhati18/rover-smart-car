"""Verify that AI forward is refused when an object is close; wheels must be lifted."""

from __future__ import annotations

import argparse
import time

from arduino_serial import ArduinoSerial
from distance_probe import parse_distance_event, wait_for_prefix


def main() -> int:
    parser = argparse.ArgumentParser(description="Test AI forward safety gate")
    parser.add_argument("--port", default="COM3")
    args = parser.parse_args()

    with ArduinoSerial(args.port) as arduino:
        wait_for_prefix(arduino, "CAPS:AI_RECOVERY_V2_DISTANCE")
        arduino.send("AI_START")
        wait_for_prefix(arduino, "AI:READY:")
        try:
            time.sleep(0.4)
            arduino.send("AI_DISTANCE")
            distance = parse_distance_event(wait_for_prefix(arduino, "AI:DIST", timeout=1.0))
            if distance is None:
                print("No valid reading; no forward request sent.")
                return 1
            print(f"Object distance: {distance} cm")
            if distance > 35:
                print("Object is too far from the 40 cm gate for a safe blocked-command test.")
                return 1

            arduino.send("AI_FORWARD")
            response = wait_for_prefix(arduino, "AI:", timeout=1.0)
            if response.startswith("AI:BLOCKED:TOO_CLOSE_CM="):
                print("PASS: Arduino blocked the forward request; no movement started.")
                return 0

            arduino.send("EMERGENCY_STOP")
            print(f"FAIL: unexpected forward response {response!r}; emergency stop sent.")
            return 1
        finally:
            arduino.send("AI_STOP")
            arduino.send("AI_RESUME")


if __name__ == "__main__":
    raise SystemExit(main())
