"""Object detection for "go to the bottle" and "follow me".

Runs a YOLOv8-nano model (COCO, 80 everyday object classes) with ONNX Runtime on the
laptop CPU. Pre-trained model, inference only; nothing is trained here.
If the runtime or the model file is missing, detect() raises a clear RuntimeError and
the rest of the dashboard keeps working.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np

WORK = Path(__file__).resolve().parent.parent
MODEL = WORK / 'models' / 'yolov8n.onnx'
PACKAGES = WORK / 'vision-packages'   # project-local install; appended so it never overrides existing packages
INPUT = 640   # the model's standard size; 320 was too small to see a bottle in a portrait frame
CONFIDENCE = 0.35
IOU = 0.45

COCO = ['person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train', 'truck', 'boat', 'traffic light',
        'fire hydrant', 'stop sign', 'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow',
        'elephant', 'bear', 'zebra', 'giraffe', 'backpack', 'umbrella', 'handbag', 'tie', 'suitcase', 'frisbee',
        'skis', 'snowboard', 'sports ball', 'kite', 'baseball bat', 'baseball glove', 'skateboard', 'surfboard',
        'tennis racket', 'bottle', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple',
        'sandwich', 'orange', 'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch',
        'potted plant', 'bed', 'dining table', 'toilet', 'tv', 'laptop', 'mouse', 'remote', 'keyboard',
        'cell phone', 'microwave', 'oven', 'toaster', 'sink', 'refrigerator', 'book', 'clock', 'vase',
        'scissors', 'teddy bear', 'hair drier', 'toothbrush']

# Spoken words -> COCO class names.
SYNONYMS = {'bottle': 'bottle', 'water bottle': 'bottle', 'cup': 'cup', 'mug': 'cup', 'glass': 'wine glass',
            'chair': 'chair', 'person': 'person', 'me': 'person', 'man': 'person', 'woman': 'person', 'human': 'person',
            'bag': 'backpack', 'backpack': 'backpack', 'school bag': 'backpack', 'handbag': 'handbag', 'purse': 'handbag',
            'phone': 'cell phone', 'mobile': 'cell phone', 'cell phone': 'cell phone', 'laptop': 'laptop',
            'book': 'book', 'ball': 'sports ball', 'football': 'sports ball', 'plant': 'potted plant',
            'tv': 'tv', 'television': 'tv', 'keyboard': 'keyboard', 'mouse': 'mouse', 'remote': 'remote',
            'bowl': 'bowl', 'teddy': 'teddy bear', 'teddy bear': 'teddy bear', 'umbrella': 'umbrella',
            'suitcase': 'suitcase', 'dog': 'dog', 'cat': 'cat', 'bed': 'bed', 'sofa': 'couch', 'couch': 'couch',
            'table': 'dining table', 'clock': 'clock', 'vase': 'vase', 'scissors': 'scissors', 'banana': 'banana',
            'apple': 'apple', 'orange': 'orange', 'toothbrush': 'toothbrush'}

_lock = threading.Lock()
_session = None


def label_for(word):
    return SYNONYMS.get(word.strip().lower())


def available():
    return MODEL.exists()


def _load():
    global _session
    if _session is None:
        if not MODEL.exists():
            raise RuntimeError('Object detection model not installed (work/models/yolov8n.onnx).')
        if str(PACKAGES) not in sys.path:
            sys.path.append(str(PACKAGES))
        try:
            import onnxruntime
        except ImportError as exc:
            raise RuntimeError('ONNX Runtime not installed (work/vision-packages).') from exc
        _session = onnxruntime.InferenceSession(str(MODEL), providers=['CPUExecutionProvider'])
    return _session


def letterbox(image):
    """Resize keeping the aspect ratio and pad to INPUT x INPUT (what YOLO expects)."""
    w, h = image.size
    scale = INPUT / max(w, h)
    nw, nh = round(w * scale), round(h * scale)
    canvas = np.full((INPUT, INPUT, 3), 114, dtype=np.uint8)
    left, top = (INPUT - nw) // 2, (INPUT - nh) // 2
    canvas[top:top + nh, left:left + nw] = np.asarray(image.convert('RGB').resize((nw, nh)))
    return canvas, scale, left, top


def nms(boxes, scores, iou=IOU):
    """Non-maximum suppression: keep the best box among strongly overlapping ones."""
    order, keep = scores.argsort()[::-1], []
    while order.size:
        i = order[0]
        keep.append(i)
        xx0 = np.maximum(boxes[i, 0], boxes[order[1:], 0]); yy0 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx1 = np.minimum(boxes[i, 2], boxes[order[1:], 2]); yy1 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        area = lambda b: (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
        overlap = inter / (area(boxes[i]) + area(boxes[order[1:]]) - inter + 1e-9)
        order = order[1:][overlap < iou]
    return keep


_worker = None


def detect(image):
    """Detection in a separate helper process, so a crash in the native ONNX Runtime library
    can never take down the dashboard server. Same results as detect_local()."""
    import io, json, struct, subprocess
    global _worker
    if not MODEL.exists():
        raise RuntimeError('Object detection model not installed (work/models/yolov8n.onnx).')
    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='JPEG', quality=90)
    data = buffer.getvalue()
    with _lock:
        for attempt in range(2):
            if _worker is None or _worker.poll() is not None:
                _worker = subprocess.Popen([sys.executable, '-B', str(Path(__file__).with_name('detector_worker.py'))],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                _worker.stdin.write(struct.pack('<I', len(data)) + data)
                _worker.stdin.flush()
                line = _worker.stdout.readline()
                if not line:
                    raise RuntimeError('detection helper stopped')
                reply = json.loads(line)
                if 'error' in reply:
                    raise RuntimeError(reply['error'])
                return [dict(d, box=tuple(d['box'])) for d in reply['detections']]
            except (OSError, ValueError, RuntimeError) as exc:
                try:
                    _worker.kill()
                except Exception:
                    pass
                _worker = None
                if attempt == 1 or 'not installed' in str(exc):
                    raise RuntimeError(f'Object detection failed ({exc}); it will restart on the next try.') from exc


def detect_local(image):
    """Return [{label, conf, box:(x0,y0,x1,y1) as 0-1 fractions of the image}], best first."""
    with _lock:
        session = _load()
        canvas, scale, left, top = letterbox(image)
        blob = canvas.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        output = session.run(None, {session.get_inputs()[0].name: blob})[0][0]   # (84, N)
    preds = output.T
    class_scores = preds[:, 4:]
    classes = class_scores.argmax(1)
    conf = class_scores[np.arange(len(classes)), classes]
    mask = conf >= CONFIDENCE
    if not mask.any():
        return []
    cx, cy, bw, bh = preds[mask, 0], preds[mask, 1], preds[mask, 2], preds[mask, 3]
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - left) / scale
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - top) / scale
    w, h = image.size
    results = []
    for c in sorted(set(classes[mask].tolist())):   # NMS per class
        idx = np.where(classes[mask] == c)[0]
        for k in nms(boxes[idx], conf[mask][idx]):
            x0, y0, x1, y1 = boxes[idx][k]
            results.append(dict(label=COCO[c], conf=round(float(conf[mask][idx][k]), 2),
                                box=(max(0.0, x0 / w), max(0.0, y0 / h), min(1.0, x1 / w), min(1.0, y1 / h))))
    return sorted(results, key=lambda r: -r['conf'])
