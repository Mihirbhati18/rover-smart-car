"""Camera-only rehearsal for an active-view recovery step.

This script never opens the Arduino serial port and cannot move the car. It
collects three fresh pictures and reports a direction only if all agree.
"""

from __future__ import annotations

import argparse
import io
import time
from pathlib import Path

from PIL import Image

from phone_camera import PhoneCamera
from vision_preview import Preview, inspect_image, make_overlay, orient_image


def consensus(previews: list[Preview]) -> tuple[str, str]:
    if len(previews) < 3:
        return "STOP", "Need three fresh pictures"
    suggestions = [preview.suggestion for preview in previews]
    if "STOP" in suggestions:
        return "STOP", "At least one picture was uncertain"
    if len(set(suggestions)) != 1:
        return "STOP", "Pictures disagreed about direction"
    return suggestions[0], "Same tentative direction in three pictures; not a safety guarantee"


def collect_live(url: str, count: int = 3) -> list[Image.Image]:
    stream_url = url.rstrip("/")
    if not stream_url.endswith("/videofeed"):
        stream_url += "/videofeed"
    images: list[Image.Image] = []
    with PhoneCamera(stream_url) as camera:
        if not camera.wait_until_ready(timeout=8):
            raise RuntimeError(f"Phone camera unavailable: {camera.status().last_error}")
        previous_time = 0.0
        for _ in range(count):
            if previous_time and not camera.wait_for_frame_after(previous_time, timeout=3):
                raise RuntimeError("No fresh camera picture arrived")
            frame, captured_at = camera.get_latest_frame()
            if frame is None or captured_at is None:
                raise RuntimeError("Phone camera picture missing")
            with Image.open(io.BytesIO(frame)) as picture:
                images.append(picture.convert("RGB"))
            previous_time = captured_at
            time.sleep(0.3)
    return images


def main() -> int:
    parser = argparse.ArgumentParser(description="Three-picture direction rehearsal; no motors")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--camera-url", help="Phone IP Webcam address")
    source.add_argument("--images", type=Path, nargs="+", help="Three saved JPEG pictures")
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    parser.add_argument("--output-dir", type=Path, default=Path("work/active_view_frames"))
    args = parser.parse_args()
    try:
        if args.images:
            if len(args.images) != 3:
                parser.error("--images requires exactly three pictures")
            images = []
            for path in args.images:
                with Image.open(path) as picture:
                    images.append(picture.convert("RGB"))
        else:
            images = collect_live(args.camera_url)

        args.output_dir.mkdir(parents=True, exist_ok=True)
        previews: list[Preview] = []
        for number, image in enumerate(images, start=1):
            oriented = orient_image(image, args.rotate)
            raw_output = args.output_dir / f"raw-view-{number}.jpg"
            oriented.save(raw_output)
            preview = inspect_image(oriented)
            previews.append(preview)
            output = args.output_dir / f"view-{number}.jpg"
            make_overlay(oriented, preview).save(output)
            print(f"Picture {number}: {preview.suggestion} | "
                  + ", ".join(f"{key.lower()} {value}%" for key, value in preview.clear_percent.items()))

        direction, reason = consensus(previews)
        print(f"THREE-PICTURE IMAGE HINT: {direction} ({reason})")
        print("DRIVING DECISION: STOP (image scorer is not validated for driving)")
        print("NO MOTOR COMMANDS SENT.")
        print(f"Pictures: {args.output_dir.resolve()}")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"PREVIEW ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
