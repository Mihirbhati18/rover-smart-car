"""One guarded forward pulse with the wheels lifted and path confirmed clear."""

from __future__ import annotations

import argparse
import time

from arduino_serial import ArduinoSerial
from distance_probe import parse_distance_event, wait_for_prefix


def main() -> int:
    parser = argparse.ArgumentParser(description="Test one 350 ms AI forward pulse")
    parser.add_argument("--port", default="COM3")
    args = parser.parse_args()

    with ArduinoSerial(args.port) as arduino:
        wait_for_prefix(arduino, "CAPS:AI_RECOVERY_V2_DISTANCE")
        arduino.send("STATUS")
        status = wait_for_prefix(arduino, "STATUS:")
        if "MODE=IDLE" not in status or "EMERGENCY=0" not in status:
            raise RuntimeError(f"Car is not idle and ready: {status}")

        try:
            arduino.send("AI_START")
            wait_for_prefix(arduino, "AI:READY:")
            time.sleep(0.4)
            arduino.send("AI_DISTANCE")
            distance = parse_distance_event(wait_for_prefix(arduino, "AI:DIST", 1.0))
            if distance is None or distance <= 45:
                print(f"No pulse: front reading is {distance} cm; need more than 45 cm.")
                return 1

            print(f"Clearance before pulse: {distance} cm")
            arduino.send("AI_FORWARD")
            wait_for_prefix(arduino, "AI:ACK:F", 1.0)
            wait_for_prefix(arduino, "AI:MOVE_DONE", 1.5)
            arduino.send("STATUS")
            status = wait_for_prefix(arduino, "STATUS:")
            if "MODE=AI_RECOVERY" not in status or "MOTION=0" not in status:
                raise RuntimeError(f"Car did not report stopped after pulse: {status}")
            print("PASS: one forward pulse completed and stopped automatically.")
            return 0
        finally:
            arduino.send("EMERGENCY_STOP")
            wait_for_prefix(arduino, "EMERGENCY:STOPPED")
            arduino.send("CLEAR_EMERGENCY")
            wait_for_prefix(arduino, "EMERGENCY:CLEARED")


if __name__ == "__main__":
    raise SystemExit(main())
