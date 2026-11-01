"""Supervised single 120 ms floor turn for angle calibration; no autonomy."""

from __future__ import annotations

import argparse

from arduino_serial import ArduinoSerial
from distance_probe import wait_for_prefix


def main() -> int:
    parser = argparse.ArgumentParser(description="One short AI-mode turn pulse")
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--direction", choices=("left", "right"), default="left")
    args = parser.parse_args()

    command, acknowledgement = (
        ("AI_LEFT_TEST", "AI:ACK:J")
        if args.direction == "left"
        else ("AI_RIGHT_TEST", "AI:ACK:K")
    )

    with ArduinoSerial(args.port) as arduino:
        wait_for_prefix(arduino, "CAPS:AI_TEST_TURN_120MS")
        arduino.send("STATUS")
        status = wait_for_prefix(arduino, "STATUS:")
        if "MODE=IDLE" not in status or "EMERGENCY=0" not in status:
            raise RuntimeError(f"Car is not idle and ready: {status}")
        try:
            arduino.send("AI_START")
            wait_for_prefix(arduino, "AI:READY:")
            arduino.send(command)
            wait_for_prefix(arduino, acknowledgement, timeout=1.0)
            wait_for_prefix(arduino, "AI:MOVE_DONE", timeout=1.5)
            arduino.send("STATUS")
            status = wait_for_prefix(arduino, "STATUS:")
            if "MODE=AI_RECOVERY" not in status or "MOTION=0" not in status:
                raise RuntimeError(f"Turn did not report stopped: {status}")
            print(f"PASS: one 120 ms {args.direction} test pulse ended automatically.")
            return 0
        finally:
            arduino.send("EMERGENCY_STOP")
            wait_for_prefix(arduino, "EMERGENCY:STOPPED")


if __name__ == "__main__":
    raise SystemExit(main())
