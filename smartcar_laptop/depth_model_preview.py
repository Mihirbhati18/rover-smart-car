"""Offline preview of official Depth Anything V2 indoor-small metric depth.

This program reads a saved photograph only. It never opens a serial port,
connects to the phone, or sends motor commands. Predicted metres must be
checked against real measurements before use in any decision.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch


SOURCE_DIR = Path(__file__).resolve().parent.parent / "vendor" / "depth-anything-v2-metric"
WEIGHTS = SOURCE_DIR / "checkpoints" / "depth_anything_v2_metric_hypersim_vits.pth"


def parse_point(value: str) -> tuple[int, int]:
    try:
        x, y = (int(part.strip()) for part in value.split(",", 1))
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError("Point must be x,y pixels") from error
    return x, y


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline camera-depth preview; no driving")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="Output side-by-side picture")
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    parser.add_argument("--point", type=parse_point, action="append", default=[],
                        help="Optional x,y pixel location in the rotated picture")
    parser.add_argument("--input-size", type=int, default=392)
    args = parser.parse_args()

    if not WEIGHTS.is_file():
        print(f"MODEL ERROR: weights missing at {WEIGHTS}")
        return 1
    picture = cv2.imread(str(args.image))
    if picture is None:
        print(f"IMAGE ERROR: cannot read {args.image}")
        return 1
    rotation_codes = {
        90: cv2.ROTATE_90_COUNTERCLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_CLOCKWISE,
    }
    if args.rotate:
        picture = cv2.rotate(picture, rotation_codes[args.rotate])

    sys.path.insert(0, str(SOURCE_DIR))
    from depth_anything_v2.dpt import DepthAnythingV2  # official source

    torch.set_num_threads(min(4, torch.get_num_threads()))
    model = DepthAnythingV2(
        encoder="vits", features=64, out_channels=[48, 96, 192, 384],
        max_depth=20,
    )
    # The official checkpoint contains a plain state_dict. weights_only avoids
    # arbitrary pickle object construction from the downloaded file.
    state = torch.load(WEIGHTS, map_location="cpu", weights_only=True)
    model.load_state_dict(state)
    model.eval()
    model.to("cpu")

    started = time.monotonic()
    with torch.inference_mode():
        depth = model.infer_image(picture, input_size=args.input_size)
    elapsed = time.monotonic() - started
    valid = depth[np.isfinite(depth) & (depth > 0)]
    if valid.size == 0:
        print("MODEL ERROR: no valid depth predictions")
        return 1

    # Red = nearer, blue = farther, clipped at 5 m for indoor readability.
    scale = np.uint8(np.clip((1.0 - depth / 5.0) * 255, 0, 255))
    heatmap = cv2.applyColorMap(scale, cv2.COLORMAP_JET)
    cv2.putText(heatmap, "MODEL ESTIMATE: red=near blue=far", (8, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

    for x, y in args.point:
        if not (0 <= x < depth.shape[1] and 0 <= y < depth.shape[0]):
            print(f"POINT ERROR: {x},{y} is outside the picture")
            return 1
        metres = float(depth[y, x])
        cv2.circle(picture, (x, y), 5, (0, 255, 255), -1)
        cv2.circle(heatmap, (x, y), 5, (0, 255, 255), -1)
        print(f"Model estimate at ({x},{y}): {metres:.2f} m")

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    combined = np.concatenate((picture, heatmap), axis=1)
    if not cv2.imwrite(str(output), combined):
        print(f"OUTPUT ERROR: could not save {output}")
        return 1
    np.save(output.with_suffix(".npy"), depth)
    print(f"MODEL OK: {depth.shape[1]}x{depth.shape[0]}, {elapsed:.1f} s on CPU")
    print(f"Predicted depth range (5th–95th percentile): "
          f"{np.percentile(valid, 5):.2f}–{np.percentile(valid, 95):.2f} m")
    print("These are uncalibrated model estimates, NOT safe driving distances.")
    print(f"Saved: {output}")
    print(f"Raw depth array: {output.with_suffix('.npy')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
