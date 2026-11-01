"""Experimental phone-image direction preview. Never sends motor commands.

The camera cannot measure distance from one RGB frame. A direction is suggested
only when a lower-image region clearly resembles the surface immediately in
front of the car; otherwise the result is STOP/uncertain.
"""

from __future__ import annotations

import argparse
import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from phone_camera import PhoneCamera


ZONES = ("LEFT", "CENTER", "RIGHT")


@dataclass(frozen=True)
class Preview:
    suggestion: str
    reason: str
    clear_percent: dict[str, int]


def inspect_image(image: Image.Image) -> Preview:
    """Compare the lower-middle image with the visible near-surface sample.

    This is a color-continuity heuristic, not object detection or depth sensing.
    """
    rgb = np.asarray(image.convert("RGB").resize((300, 240)), dtype=np.float32)
    brightness = rgb.mean(axis=2)
    if brightness.mean() < 25 or rgb.std() < 8:
        return Preview("STOP", "Image is dark or lacks visible detail", dict.fromkeys(ZONES, 0))

    scores: dict[str, int] = {}
    for index, zone in enumerate(ZONES):
        x0, x1 = index * 100 + 12, (index + 1) * 100 - 12
        near = rgb[197:225, x0:x1]
        # Focus below the horizon. Higher pixels may be wall/sky rather than
        # travel surface and would falsely make an open zone look blocked.
        probe = rgb[150:190, x0:x1]
        reference = np.median(near.reshape(-1, 3), axis=0)
        # RGB distance allows ordinary floor texture, but rejects strongly
        # contrasting objects. Each zone gets its own near-surface reference.
        distance = np.linalg.norm(probe - reference, axis=2)
        scores[zone] = round(float(np.mean(distance < 65) * 100))

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, runner_up = ranked[0], ranked[1]
    if best[1] < 68:
        return Preview("STOP", "No zone clearly matches the near surface", scores)
    if best[1] - runner_up[1] < 10:
        return Preview("STOP", "No clear advantage between directions", scores)
    # Keep straight travel distinct from turns: a turn is only a visual hint.
    return Preview(best[0], "Tentative image-only suggestion; verify by eye", scores)


def make_overlay(image: Image.Image, preview: Preview) -> Image.Image:
    result = image.convert("RGB").copy()
    draw = ImageDraw.Draw(result)
    width, height = result.size
    for index, zone in enumerate(ZONES):
        x0 = round(width * (index + 0.12) / 3)
        x1 = round(width * (index + 0.88) / 3)
        y0, y1 = round(height * 150 / 240), round(height * 190 / 240)
        color = "#00d070" if zone == preview.suggestion else "#ffbc30"
        draw.rectangle((x0, y0, x1, y1), outline=color, width=3)
        draw.rectangle((x0, y0 - 20, x0 + 95, y0), fill="black")
        draw.text((x0 + 3, y0 - 18), f"{zone} {preview.clear_percent[zone]}%", fill="white")
    draw.rectangle((0, 0, width, 26), fill="black")
    draw.text((5, 6), f"PREVIEW ONLY: {preview.suggestion} - NO MOTOR COMMAND", fill="white")
    return result


def load_camera_image(url: str) -> Image.Image:
    stream_url = url.rstrip("/")
    if not stream_url.endswith("/videofeed"):
        stream_url += "/videofeed"
    with PhoneCamera(stream_url) as camera:
        if not camera.wait_until_ready(timeout=8):
            raise RuntimeError(f"Phone camera unavailable: {camera.status().last_error}")
        frame, _ = camera.get_latest_frame()
        if frame is None:
            raise RuntimeError("Phone camera did not provide a picture")
        with Image.open(io.BytesIO(frame)) as source:
            return source.convert("RGB")


def orient_image(image: Image.Image, degrees: int) -> Image.Image:
    """Rotate an image counterclockwise to match the mounted phone."""
    if degrees not in (0, 90, 180, 270):
        raise ValueError("Rotation must be 0, 90, 180, or 270 degrees")
    return image.rotate(degrees, expand=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview a phone-camera direction; no driving")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path, help="Existing JPEG picture")
    source.add_argument("--camera-url", help="Phone IP Webcam address")
    parser.add_argument("--overlay", type=Path, help="Save marked-up JPEG")
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0,
                        help="Correct the mounted phone's image orientation")
    args = parser.parse_args()
    try:
        if args.image:
            with Image.open(args.image) as file_image:
                image = file_image.convert("RGB")
        else:
            image = load_camera_image(args.camera_url)
        image = orient_image(image, args.rotate)
        preview = inspect_image(image)
        print(f"PREVIEW ONLY: {preview.suggestion} ({preview.reason})")
        print("Visual surface match: " + ", ".join(
            f"{zone.lower()} {preview.clear_percent[zone]}%" for zone in ZONES
        ))
        if args.overlay:
            args.overlay.parent.mkdir(parents=True, exist_ok=True)
            make_overlay(image, preview).save(args.overlay)
            print(f"Marked picture: {args.overlay.resolve()}")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"PREVIEW ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
