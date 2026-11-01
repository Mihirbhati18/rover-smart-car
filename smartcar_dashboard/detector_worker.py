"""Detection helper process: reads length-prefixed JPEG frames on stdin, writes one JSON line per frame.

Started by detector.detect(). Running the native ONNX Runtime here means a crash only ends
this helper, never the dashboard server.
"""
import io
import json
import struct
import sys

from PIL import Image

import detector


def main():
    stdin, stdout = sys.stdin.buffer, sys.stdout
    while True:
        header = stdin.read(4)
        if len(header) < 4:
            return
        data = stdin.read(struct.unpack('<I', header)[0])
        try:
            detections = detector.detect_local(Image.open(io.BytesIO(data)))
            reply = {'detections': [dict(d, box=[float(v) for v in d['box']]) for d in detections]}
        except Exception as exc:  # report, keep serving
            reply = {'error': str(exc)}
        stdout.write(json.dumps(reply) + '\n')
        stdout.flush()


if __name__ == '__main__':
    main()
