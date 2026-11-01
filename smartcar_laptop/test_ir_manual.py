"""Observe the original IR Up and OK buttons with wheels lifted."""

from __future__ import annotations

import time

from arduino_serial import ArduinoSerial


def main() -> int:
    with ArduinoSerial("COM3") as arduino:
        print("READY: press IR Up, then OK/Stop within 15 seconds", flush=True)
        saw_up = False
        saw_ok = False
        deadline = time.monotonic() + 15.0
        try:
            while time.monotonic() < deadline:
                event = arduino.get_event(timeout=0.2)
                if event is None:
                    continue
                print(f"Arduino: {event.text}", flush=True)
                if event.text == "UP":
                    saw_up = True
                if event.text == "OK" and saw_up:
                    saw_ok = True
                    break
            arduino.send("STATUS")
            check_deadline = time.monotonic() + 2.0
            while time.monotonic() < check_deadline:
                event = arduino.get_event(timeout=0.2)
                if event and event.text.startswith("STATUS:"):
                    print(f"Arduino: {event.text}", flush=True)
                    break
        finally:
            arduino.send("EMERGENCY_STOP")
            time.sleep(0.1)
            arduino.send("CLEAR_EMERGENCY")
            arduino.send("STOP")

    if saw_up and saw_ok:
        print("IR UP AND OK TEST PASSED")
        return 0
    print("IR button sequence incomplete")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
