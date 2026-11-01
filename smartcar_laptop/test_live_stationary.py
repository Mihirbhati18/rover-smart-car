"""Stationary end-to-end check for the AI recovery sketch and phone camera."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from arduino_serial import ArduinoSerial
from phone_camera import PhoneCamera
from recovery_capture import RecoveryCaptureCoordinator


def wait_for(arduino: ArduinoSerial, prefix: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = arduino.get_event(timeout=0.2)
        if event is None:
            continue
        print(f"Arduino: {event.text}")
        if event.text.startswith(prefix):
            return event.text
    raise TimeoutError(f"Arduino did not report {prefix!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--camera-url", required=True)
    parser.add_argument(
        "--test-pulse",
        action="store_true",
        help="Run one 350 ms forward pulse with wheels lifted",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "recovery_frames",
    )
    args = parser.parse_args()

    with PhoneCamera(args.camera_url) as camera:
        if not camera.wait_until_ready(timeout=10.0):
            raise RuntimeError(f"Camera unavailable: {camera.status().last_error}")
        print("Camera ready")

        with ArduinoSerial(args.port) as arduino:
            wait_for(arduino, "CAPS:AI_RECOVERY_V")
            arduino.send("STATUS")
            first_status = wait_for(arduino, "STATUS:")
            if "MODE=IDLE" not in first_status:
                raise RuntimeError(f"Expected IDLE before test, got {first_status}")

            arduino.send("AI_START")
            wait_for(arduino, "STUCK:SERIAL")
            coordinator = RecoveryCaptureCoordinator(
                arduino, camera, args.output_dir
            )
            saved = coordinator.handle_arduino_event("STUCK:SERIAL")
            if saved is None:
                raise RuntimeError("Recovery request was ignored")
            print(f"Fresh recovery frame: {saved}")

            arduino.send("AI_RESUME")
            wait_for(arduino, "AI:RESUMED:IDLE")
            arduino.send("EMERGENCY_STOP")
            wait_for(arduino, "EMERGENCY:STOPPED")
            arduino.send("STATUS")
            latched = wait_for(arduino, "STATUS:")
            if "EMERGENCY=1" not in latched:
                raise RuntimeError(f"Emergency latch failed: {latched}")

            arduino.send("AI_FORWARD")
            wait_for(arduino, "ERROR:EMERGENCY_LATCHED")

            arduino.send("CLEAR_EMERGENCY")
            wait_for(arduino, "EMERGENCY:CLEARED")
            arduino.send("STATUS")
            cleared = wait_for(arduino, "STATUS:")
            if "EMERGENCY=0" not in cleared or "MODE=IDLE" not in cleared:
                raise RuntimeError(f"Emergency clear failed: {cleared}")

            if args.test_pulse:
                try:
                    arduino.send("AI_START")
                    wait_for(arduino, "STUCK:SERIAL")
                    time.sleep(0.4)  # Give the forward-facing sensor time to centre.
                    arduino.send("AI_FORWARD")
                    wait_for(arduino, "AI:ACK:F")
                    wait_for(arduino, "AI:MOVE_DONE")
                    arduino.send("STATUS")
                    stopped = wait_for(arduino, "STATUS:")
                    if "MODE=AI_RECOVERY" not in stopped or "MOTION=0" not in stopped:
                        raise RuntimeError(f"Pulse did not stop correctly: {stopped}")
                    arduino.send("AI_RESUME")
                    wait_for(arduino, "AI:RESUMED:IDLE")
                finally:
                    arduino.send("EMERGENCY_STOP")
                    wait_for(arduino, "EMERGENCY:STOPPED")
                    arduino.send("CLEAR_EMERGENCY")
                    wait_for(arduino, "EMERGENCY:CLEARED")

    print("RECOVERY TEST PASSED" if args.test_pulse else "STATIONARY RECOVERY TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
