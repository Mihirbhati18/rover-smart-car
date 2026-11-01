"""Check mode selection and STOP after uploading the recovery sketch."""

from __future__ import annotations

import time

from arduino_serial import ArduinoSerial


def wait_for_status(arduino: ArduinoSerial, expected: str) -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        event = arduino.get_event(timeout=0.2)
        if event is None:
            continue
        print(f"Arduino: {event.text}", flush=True)
        if event.text.startswith("STATUS:"):
            if f"MODE={expected}" not in event.text:
                raise RuntimeError(f"Expected {expected}, got {event.text}")
            return
    raise TimeoutError(f"No status for {expected}")


def main() -> int:
    with ArduinoSerial("COM3") as arduino:
        try:
            for command, mode in (
                ("LINE_TRACKING", "LINE"),
                ("OBSTACLE_AVOIDANCE", "OBSTACLE"),
            ):
                print(f"Testing {mode} mode briefly", flush=True)
                arduino.send(command)
                time.sleep(0.25)
                arduino.send("STATUS")
                wait_for_status(arduino, mode)
                arduino.send("STOP")
                arduino.send("STATUS")
                wait_for_status(arduino, "BLUETOOTH")
        finally:
            arduino.send("EMERGENCY_STOP")
            time.sleep(0.1)
            arduino.send("CLEAR_EMERGENCY")
            arduino.send("STOP")
    print("EXISTING MODE SWITCH TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
