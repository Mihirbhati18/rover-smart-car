"""Test brief AI recovery motor pulses with the robot's wheels lifted."""

from __future__ import annotations

import time

from arduino_serial import ArduinoSerial


def wait_for(arduino: ArduinoSerial, prefix: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = arduino.get_event(timeout=0.2)
        if event is None:
            continue
        print(f"Arduino: {event.text}", flush=True)
        if event.text.startswith(prefix):
            return event.text
    raise TimeoutError(f"Missing Arduino response: {prefix}")


def main() -> int:
    with ArduinoSerial("COM3") as arduino:
        wait_for(arduino, "CAPS:AI_RECOVERY_V")
        arduino.send("STATUS")
        status = wait_for(arduino, "STATUS:")
        if "MODE=IDLE" not in status or "EMERGENCY=0" not in status:
            raise RuntimeError(f"Car is not ready for pulse test: {status}")

        try:
            arduino.send("AI_START")
            wait_for(arduino, "STUCK:SERIAL")

            for direction, letter in (
                ("BACKWARD", "B"),
                ("LEFT", "L"),
                ("RIGHT", "R"),
            ):
                print(f"Testing {direction} for 350 ms", flush=True)
                arduino.send(f"AI_{direction}")
                wait_for(arduino, f"AI:ACK:{letter}")
                wait_for(arduino, "AI:MOVE_DONE")
                arduino.send("STATUS")
                status = wait_for(arduino, "STATUS:")
                if "MODE=AI_RECOVERY" not in status or "MOTION=0" not in status:
                    raise RuntimeError(f"Motor did not stop: {status}")
                time.sleep(0.6)

            arduino.send("AI_RESUME")
            wait_for(arduino, "AI:RESUMED:IDLE")
        finally:
            arduino.send("EMERGENCY_STOP")
            wait_for(arduino, "EMERGENCY:STOPPED")
            arduino.send("CLEAR_EMERGENCY")
            wait_for(arduino, "EMERGENCY:CLEARED")

    print("THREE MOTOR DIRECTION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
