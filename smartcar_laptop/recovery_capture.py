"""Connect an Arduino STUCK event to a safe recovery-frame capture."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Protocol


_WORK_DIR = Path(__file__).resolve().parent.parent


class CommandSender(Protocol):
    def send(self, command: str) -> None: ...
    def get_event(self, timeout: float | None = None) -> object | None: ...


class FrameSource(Protocol):
    def wait_for_frame_after(
        self, after_time: float, timeout: float = 3.0, max_age: float = 1.0
    ) -> bool: ...
    def save_latest(self, output_path: str | Path) -> Path: ...


class RecoveryCaptureCoordinator:
    """React only to STUCK events; unrelated Arduino messages are ignored."""

    def __init__(
        self,
        arduino: CommandSender,
        camera: FrameSource,
        output_dir: str | Path,
    ) -> None:
        self.arduino = arduino
        self.camera = camera
        self.output_dir = Path(output_dir).resolve()

    def handle_arduino_event(self, event_text: str) -> Path | None:
        if not event_text.strip().upper().startswith("STUCK:"):
            return None

        # Keep the car stationary before obtaining the decision frame.
        self.arduino.send("AI_STOP")
        deadline = time.monotonic() + 2.0
        acknowledged = False
        while time.monotonic() < deadline:
            event = self.arduino.get_event(timeout=0.2)
            if event is None:
                continue
            if getattr(event, "text", "") == "AI:ACK:S":
                acknowledged = True
                break
            if getattr(event, "text", "").startswith("AI:ERROR:"):
                raise RuntimeError(getattr(event, "text"))
        if not acknowledged:
            raise TimeoutError("Arduino did not confirm AI STOP.")

        if not self.camera.wait_for_frame_after(time.time(), timeout=3.0):
            raise TimeoutError("No fresh camera frame arrived after AI STOP.")

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = self.output_dir / f"recovery-{timestamp}.jpg"
        return self.camera.save_latest(output_path)


class OfflineCamera:
    """Use one saved photograph to test the event workflow without a phone."""

    def __init__(self, image_path: str | Path) -> None:
        self.image_path = Path(image_path).resolve()
        self.frame = self.image_path.read_bytes()
        if not self.frame.startswith(b"\xff\xd8"):
            raise ValueError(f"Could not read test image: {self.image_path}")

    def save_latest(self, output_path: str | Path) -> Path:
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.frame)
        return path

    def wait_for_frame_after(
        self, after_time: float, timeout: float = 3.0, max_age: float = 1.0
    ) -> bool:
        return True


class RecordingArduino:
    """Non-hardware command recorder used by the offline test."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._events: list[object] = []

    def send(self, command: str) -> None:
        self.commands.append(command)
        if command == "AI_STOP":
            self._events.append(type("Event", (), {"text": "AI:ACK:S"})())

    def get_event(self, timeout: float | None = None) -> object | None:
        return self._events.pop(0) if self._events else None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a camera frame when Arduino reports STUCK."
    )
    parser.add_argument("--port", default="COM3")
    parser.add_argument(
        "--camera-url",
        default="http://192.168.1.4:8080/videofeed",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_WORK_DIR / "recovery_frames",
    )
    parser.add_argument(
        "--offline-image",
        type=Path,
        help="Test with a saved image and no phone or Arduino movement",
    )
    parser.add_argument(
        "--simulate-stuck",
        action="store_true",
        help="Generate a test STUCK event",
    )
    return parser.parse_args()


def _run_offline(args: argparse.Namespace) -> int:
    arduino = RecordingArduino()
    camera = OfflineCamera(args.offline_image)
    coordinator = RecoveryCaptureCoordinator(arduino, camera, args.output_dir)

    saved = coordinator.handle_arduino_event("STUCK:OFFLINE_TEST")
    assert saved is not None
    print("OFFLINE RECOVERY CAPTURE OK")
    print(f"Safety command: {arduino.commands[0]}")
    print(f"Saved: {saved}")
    return 0


def _run_live(args: argparse.Namespace) -> int:
    # Hardware dependencies are imported only for a live run. This lets the
    # offline workflow test run even when the phone and serial port are absent.
    from arduino_serial import ArduinoSerial
    from phone_camera import PhoneCamera

    with PhoneCamera(args.camera_url) as camera:
        if not camera.wait_until_ready(timeout=10.0):
            print("CAMERA ERROR: no live frame received")
            return 1

        with ArduinoSerial(args.port) as arduino:
            coordinator = RecoveryCaptureCoordinator(arduino, camera, args.output_dir)
            if args.simulate_stuck:
                saved = coordinator.handle_arduino_event("STUCK:SIMULATED")
                print(f"RECOVERY CAPTURE OK: {saved}")
                return 0

            print("READY: waiting for an Arduino STUCK event (Ctrl+C to stop)")
            while True:
                event = arduino.get_event(timeout=0.5)
                if event is None:
                    continue
                print(f"Arduino: {event.text}")
                saved = coordinator.handle_arduino_event(event.text)
                if saved is not None:
                    print(f"RECOVERY FRAME SAVED: {saved}")


def main() -> int:
    args = _parse_args()
    if args.offline_image:
        return _run_offline(args)
    try:
        return _run_live(args)
    except KeyboardInterrupt:
        print("Stopped safely.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
