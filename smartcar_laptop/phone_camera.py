"""Keep the newest JPEG frame from Android IP Webcam's MJPEG stream."""

from __future__ import annotations

import argparse
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CameraStatus:
    connected: bool
    frame_count: int
    last_frame_time: float | None
    last_error: str | None


class PhoneCamera:
    def __init__(self, stream_url: str, reconnect_delay: float = 1.0) -> None:
        self.stream_url = stream_url
        self.reconnect_delay = reconnect_delay
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_frame: bytes | None = None
        self._last_frame_time: float | None = None
        self._frame_count = 0
        self._connected = False
        self._last_error: str | None = None

    def start(self) -> "PhoneCamera":
        if self._thread and self._thread.is_alive():
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=6.0)
        with self._lock:
            self._connected = False

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status().frame_count:
                return True
            time.sleep(0.05)
        return False

    def get_latest_frame(self) -> tuple[bytes | None, float | None]:
        """Return immutable JPEG bytes and the time they reached the laptop."""
        with self._lock:
            return self._latest_frame, self._last_frame_time

    def wait_for_frame_after(
        self, after_time: float, timeout: float = 3.0, max_age: float = 1.0
    ) -> bool:
        """Wait for a frame received after the specified event time."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame, captured_at = self.get_latest_frame()
            now = time.time()
            if frame is not None and captured_at is not None:
                if captured_at > after_time and now - captured_at <= max_age:
                    return True
            time.sleep(0.02)
        return False

    def save_latest(self, output_path: str | Path) -> Path:
        frame, _ = self.get_latest_frame()
        if frame is None:
            raise RuntimeError("No camera frame is available yet.")
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(frame)
        return path

    def status(self) -> CameraStatus:
        with self._lock:
            return CameraStatus(
                self._connected,
                self._frame_count,
                self._last_frame_time,
                self._last_error,
            )

    def _read_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                request = urllib.request.Request(
                    self.stream_url, headers={"User-Agent": "SmartCarCamera/1.0"}
                )
                with urllib.request.urlopen(request, timeout=5.0) as stream:
                    with self._lock:
                        self._connected = True
                        self._last_error = None
                    self._read_frames(stream)
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._connected = False
                    self._last_error = str(exc)
            if not self._stop_event.is_set():
                self._stop_event.wait(self.reconnect_delay)

    def _read_frames(self, stream: object) -> None:
        buffer = bytearray()
        while not self._stop_event.is_set():
            chunk = stream.read(4096)  # type: ignore[attr-defined]
            if not chunk:
                raise ConnectionError("Camera stream closed")
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    if len(buffer) > 8_000_000:
                        buffer.clear()
                    break
                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                with self._lock:
                    self._latest_frame = frame
                    self._last_frame_time = time.time()
                    self._frame_count += 1

    def __enter__(self) -> "PhoneCamera":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Test and save the latest phone frame")
    parser.add_argument("--url", required=True, help="IP Webcam /videofeed URL")
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--save", type=Path, required=True)
    args = parser.parse_args()

    with PhoneCamera(args.url) as camera:
        if not camera.wait_until_ready():
            print(f"CAMERA ERROR: {camera.status().last_error}")
            return 1
        time.sleep(max(args.duration, 0))
        status = camera.status()
        saved = camera.save_latest(args.save)
        frame, captured_at = camera.get_latest_frame()
    age_ms = (time.time() - captured_at) * 1000 if captured_at else float("nan")
    print(f"CAMERA OK: {status.frame_count} frames, latest age {age_ms:.0f} ms")
    print(f"Saved {len(frame or b'')} bytes to {saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
