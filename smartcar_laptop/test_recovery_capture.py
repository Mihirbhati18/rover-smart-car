from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recovery_capture import OfflineCamera, RecordingArduino, RecoveryCaptureCoordinator


class SilentArduino(RecordingArduino):
    def send(self, command: str) -> None:
        self.commands.append(command)


class StaleCamera(OfflineCamera):
    def wait_for_frame_after(
        self, after_time: float, timeout: float = 3.0, max_age: float = 1.0
    ) -> bool:
        return False


class RecoveryCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.image = Path(__file__).resolve().parent.parent / "active_view_frames" / "bottle-centered-2026-10-10" / "raw-view-1.jpg"
        self.temp_root = Path(__file__).resolve().parent.parent / "test-temp"
        self.temp_root.mkdir(exist_ok=True)

    def test_stop_ack_then_capture(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            arduino = RecordingArduino()
            coordinator = RecoveryCaptureCoordinator(
                arduino, OfflineCamera(self.image), directory
            )
            saved = coordinator.handle_arduino_event("STUCK:IR_BUTTON_3")
            self.assertIsNotNone(saved)
            self.assertEqual(saved.read_bytes(), self.image.read_bytes())
            self.assertEqual(arduino.commands, ["AI_STOP"])

    def test_no_ack_never_captures(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            coordinator = RecoveryCaptureCoordinator(
                SilentArduino(), OfflineCamera(self.image), directory
            )
            with self.assertRaises(TimeoutError):
                coordinator.handle_arduino_event("STUCK:IR_BUTTON_3")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_stale_camera_never_captures(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            coordinator = RecoveryCaptureCoordinator(
                RecordingArduino(), StaleCamera(self.image), directory
            )
            with self.assertRaises(TimeoutError):
                coordinator.handle_arduino_event("STUCK:IR_BUTTON_3")
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
