# Object detection model

Object finding, “follow me” and “what do you see” need `yolov8n.onnx` in this folder.
The weights are not committed (Ultralytics YOLOv8 weights are AGPL-3.0 and 12 MB).

Export them once (needs `pip install ultralytics`):

```bash
cd models
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=640, opset=12)"
```

Without the model the rest of the dashboard works normally; detection commands reply
“My object detection is not installed.”
