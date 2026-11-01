"""Wait for IR button 3, then capture a fresh camera frame while stopped."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from arduino_serial import ArduinoSerial
from phone_camera import PhoneCamera
from recovery_capture import RecoveryCaptureCoordinator


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--camera-url", required=True)
    parser.add_argument("--wait-seconds", type=float, default=45.0)
    args = parser.parse_args()

    with PhoneCamera(args.camera_url) as camera:
        if not camera.wait_until_ready(timeout=10.0):
            print("Camera unavailable")
            return 1
        with ArduinoSerial(args.port) as arduino:
            arduino.send("STATUS")
            deadline = time.monotonic() + args.wait_seconds
            print("READY: press IR remote button 3 now", flush=True)
            while time.monotonic() < deadline:
                event = arduino.get_event(timeout=0.3)
                if event is None:
                    continue
                print(f"Arduino: {event.text}", flush=True)
                if event.text == "STUCK:IR_BUTTON_3":
                    coordinator = RecoveryCaptureCoordinator(
                        arduino,
                        camera,
                        Path(__file__).resolve().parent.parent / "recovery_frames",
                    )
                    saved = coordinator.handle_arduino_event(event.text)
                    print(f"IR RECOVERY CAPTURE OK: {saved}", flush=True)
                    arduino.send("AI_RESUME")
                    return 0
            print("IR button 3 was not detected within the test window.")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
