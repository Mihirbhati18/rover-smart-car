"""Local-only SmartCar presentation dashboard. Motor commands require explicit arming."""
from __future__ import annotations

import argparse
import io
import ipaddress
import json
import math
import re
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
WORK = ROOT.parent
sys.path.insert(0, str(WORK / 'smartcar_laptop'))
from PIL import Image, ImageDraw, ImageFont
from arduino_serial import ArduinoSerial
from phone_camera import PhoneCamera
from side_scan_probe import parse_scan_event
from vision_preview import inspect_image, make_overlay, orient_image
from vr import VRController

REPLAY = WORK / 'active_view_frames/bottle-centered-2026-10-10/raw-view-1.jpg'
TOKEN = secrets.token_urlsafe(32)


class ControlsOff(RuntimeError):
    """A voice command arrived while controls were OFF; nothing was sent."""


def camera_address(value):
    value = value.strip()
    if '://' not in value:
        value = 'http://' + value
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or url.username or url.password:
        raise ValueError('Enter the local IP Webcam address without a username or password.')
    try:
        address = ipaddress.ip_address(url.hostname or '')
    except ValueError:
        raise ValueError('Use the phone IP address shown in IP Webcam, such as 192.168.1.4:8080.')
    if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError('Use the phone’s private Wi-Fi IP address.')
    port = url.port or (443 if url.scheme == 'https' else 80)
    host = f'[{address}]' if address.version == 6 else str(address)
    return f'{url.scheme}://{host}:{port}/videofeed'


def voice_direction(text):
    words = ' '.join(re.findall(r'[a-z]+', text.lower()))
    if re.search(r'\b(stop|halt|freeze|emergency)\b', words):
        return 'STOP'
    if re.search(r'\b(not|no|never|don|dont)\b', words):
        return None
    words = re.sub(r'\b(please|car|robot|move|go|turn|the)\b', '', words).strip()
    words = ' '.join(words.split())
    return {'forward': 'FORWARD', 'ahead': 'FORWARD', 'straight': 'FORWARD',
            'back': 'BACKWARD', 'backward': 'BACKWARD', 'backwards': 'BACKWARD',
            'reverse': 'BACKWARD', 'left': 'LEFT', 'right': 'RIGHT'}.get(words)


def decide(scores, scan):
    """Experimental image hint, vetoed by measured clearance. Never uses camera cm."""
    ranked = sorted(scores, key=scores.get, reverse=True)
    best = ranked[0]
    if scores[best] < 75:
        return 'STOP', 'Image has no convincing floor-like region.'
    if scores[best] - scores[ranked[1]] < 12:
        if scores['CENTER'] >= 90:
            best = 'CENTER'
        else:
            return 'STOP', 'Image directions are too similar to choose.'
    direction = 'FORWARD' if best == 'CENTER' else best
    if not scan or any(scan.get(k) is None for k in ('left', 'centre', 'right')):
        return 'STOP', f'Image hints {direction.lower()}; valid ultrasonic readings are required.'
    if direction == 'FORWARD' and scan['centre'] <= 55:
        return 'STOP', 'Front clearance is 55 cm or less; forward step blocked.'
    if direction in ('LEFT', 'RIGHT'):
        if scan[direction.lower()] <= 45 or scan['centre'] <= 25:
            return 'STOP', 'Ultrasonic clearance is too small for the proposed turn.'
    return direction, f'Image hints {direction.lower()}; the measured clearance check passed.'


def route_plan(text, manual=False):
    """Strict complete parse: never silently execute an unrecognized clause."""
    text = text.lower().strip()
    if re.search(r'\d\.\d', text): raise ValueError('Use whole centimetres and whole degrees.')
    numbers = {'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,
               'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12,'fifteen':15,
               'twenty':20,'thirty':30,'forty':40,'fifty':50,'sixty':60,
               'seventy':70,'eighty':80,'ninety':90}
    for word, value in numbers.items():
        text = re.sub(r'\b'+word+r'\b', str(value), text)
    text = re.sub(r'\b(20|30|40|50|60|70|80|90)[ -]+([1-9])\b',
                  lambda m: str(int(m[1])+int(m[2])), text)
    text = re.sub(r'\bcentimet(?:er|re)s?\b', 'cm', text)
    text = re.sub(r'\bdegrees?\b|°', 'deg', text)
    text = re.sub(r'\s+', ' ', text.replace('.', '')).strip()
    clauses = [c.strip() for c in re.split(r'\s*[,;]\s*(?:(?:and then|then|and)\s+)?|\s+(?:and then|then|and)\s+', text)]
    plan = []
    for index, clause in enumerate(clauses):
        if clause == 'stop':
            if index != len(clauses)-1:
                raise ValueError('Stop must be the final instruction; no movement after stop.')
            continue
        clause = re.sub(r'^(?:please )?(?:(?:move|go|take|turn) )?(?:a )?', '', clause)
        clause = re.sub(r'\s+again$', '', clause)
        forward = re.fullmatch(r'(?:forward (\d+) cm|(\d+) cm forward)', clause)
        reverse = re.fullmatch(r'(?:(?:backward|back|reverse) (\d+) cm|(\d+) cm (?:backward|back|reverse))', clause) if manual else None
        turn = re.fullmatch(r'(left|right)(?: (\d+) deg)?(?: turn)?', clause)
        if forward:
            value = int(forward[1] or forward[2])
            if value < 1 or (not manual and value > 100): raise ValueError('Use a positive distance in whole cm; camera-mode steps are limited to 100 cm.')
            plan.append(dict(direction='FORWARD', value=value, unit='cm'))
        elif reverse:
            value = int(reverse[1] or reverse[2])
            if value < 1: raise ValueError('Use a positive reverse distance in whole cm.')
            plan.append(dict(direction='BACKWARD', value=value, unit='cm'))
        elif turn:
            value = int(turn[2] or 90)
            if value < 1 or (not manual and value > 180): raise ValueError('Use positive whole degrees; camera-mode turns are limited to 180 degrees.')
            plan.append(dict(direction=turn[1].upper(), value=value, unit='deg'))
        else:
            raise ValueError('Use: forward 30 cm, then right 30 degrees, then forward 20 cm, then stop. Reverse is not supported in routes.')
    if not plan or (not manual and len(plan) > 6): raise ValueError('Enter movement instructions; camera mode supports 1–6 per route.')
    duration = sum(s['value']*(70 if s['unit']=='cm' else 12) for s in plan)
    if not manual and duration > 20000: raise ValueError('Route is too long; split it into shorter routes.')
    return plan


# Pass-obstacle mode: drive a distance, steering around whatever blocks the centre.
# Position is dead-reckoned from timing: x = cm along the original line, y = cm to the right of it.
PASS_SAFETY_CM = 30       # default: centre ultrasonic at or below this counts as blocked
PASS_IMAGE_CLEAR = 70     # centre floor match below this counts as blocked
PASS_IMAGE_CLEAR_NO_ECHO = 85  # stricter image check when the ultrasonic has no echo
PASS_TURN_DEG = 10        # default turn per check
PASS_STEP_CM = 10
PASS_MAX_HEADING = 60
PASS_CLEAR_MARGIN_CM = 20  # go this far past an obstacle (along the line) before heading back
PASS_MIN_STEP_CM = 3
PASS_MAX_GOAL_ANGLE = 45   # steepest angle used to rejoin the line
PASS_GOAL_TOLERANCE_CM = 5
PASS_IMAGE_OBSTACLE_CM = 30  # where an image-only block is drawn on the track
PASS_MAX_CYCLES = 120
PASS_MAX_SECONDS = 300


def pass_distance(text):
    text = text.lower().strip()
    match = re.fullmatch(r'(?:(?:please |move |go )*forward(?: by)? )?(\d+) ?(?:cm|centimet(?:er|re)s?)?', text)
    if not match:
        raise ValueError('Say or type a distance, for example: forward 200 cm.')
    value = int(match[1])
    if not 1 <= value <= 500:
        raise ValueError('Use a whole distance from 1 to 500 cm.')
    return value


def pass_blocked(distance, scores, safety_cm=PASS_SAFETY_CM):
    """Return a reason if the way straight ahead is blocked, else None."""
    if distance is not None and distance <= safety_cm:
        return f'ultrasonic {distance} cm ≤ {safety_cm} cm'
    limit = PASS_IMAGE_CLEAR if distance is not None else PASS_IMAGE_CLEAR_NO_ECHO
    if scores['CENTER'] < limit:
        return f"image centre {scores['CENTER']}% < {limit}%"
    return None


def pass_turn_side(avoid_side, scores):
    """Keep avoiding on the side already chosen; otherwise pick the clearer image side."""
    if avoid_side:
        return avoid_side
    return 'RIGHT' if scores['RIGHT'] >= scores['LEFT'] else 'LEFT'


def pass_goal_heading(x, y, target):
    """Heading (degrees, + = right) from (x, y) toward the destination (target, 0)."""
    angle = math.degrees(math.atan2(-y, max(target - x, 1)))
    return max(-PASS_MAX_GOAL_ANGLE, min(PASS_MAX_GOAL_ANGLE, angle))


def route_clearance(direction, scan):
    keys = ('centre',) if direction == 'FORWARD' else ('centre', direction.lower())
    if not scan or any(scan.get(k) is None for k in keys):
        raise RuntimeError('Route blocked: required ultrasonic reading is invalid.')
    if any(scan[k] <= 20 for k in keys):
        raise RuntimeError('Route blocked: required ultrasonic clearance is 20 cm or less.')


def route_pulses(step):
    # User-observed initial estimates: 5 cm / 350 ms; 10 degrees / 120 ms.
    remaining = step['value'] * (70 if step['unit']=='cm' else 12)
    limit = 350 if step['unit']=='cm' else 120
    while remaining:
        duration = min(limit, remaining)
        yield duration
        remaining -= duration


class Dashboard:
    def __init__(self):
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.send_lock = threading.RLock()
        self.events = deque(maxlen=100)
        self.received = deque(maxlen=600)
        self.rx_seq = 0
        self.epoch = 0
        self.link = None
        self.camera = None
        self.overlay = None
        self.last_heartbeat = time.monotonic()
        self.state = dict(source='replay', camera_url='', camera_connected=False,
                          serial_connected=False, port='COM3', transport='usb', mode='REPLAY',
                          armed=False, manual_only=False, emergency=False, busy=False, auto=False,
                          rotation=180, crop_bottom=20, boxes=True, analysis=None, scan=None, last_command='—',
                          firmware=False, route_firmware=False, manual_firmware=False, warning='', route_plan=None, route_text='',
                          route_status='Preview a route first', pass_status='Not running', pass_track=None, error='', stage='Ready', voice_text='', operation='')
        self.log('SYSTEM', 'Presentation console ready. Recorded demo is selected; hardware is disconnected.')
        self.vr_quiet = False  # VR polls the sonar often; keep those lines out of the timeline
        self.vr = VRController(self)
        threading.Thread(target=self.watchdog, daemon=True).start()

    def log(self, kind, message):
        with self.lock:
            self.events.append(dict(id=secrets.token_hex(4), time=time.strftime('%H:%M:%S'),
                                    kind=kind, message=str(message)))

    def snapshot(self):
        with self.lock:
            result = dict(self.state)
            result['events'] = list(self.events)
        if self.link and not self.link.is_open:
            result.update(serial_connected=False, armed=False)
        if self.camera:
            status = self.camera.status()
            result['camera_connected'] = bool(status.last_frame_time and time.time()-status.last_frame_time < 2)
            result['frame_age'] = round(time.time()-status.last_frame_time, 1) if status.last_frame_time else None
        return result

    def send(self, command):
        with self.send_lock:
            if not self.link or not self.link.is_open:
                raise RuntimeError('Arduino is disconnected.')
            self.link.send(command)
        self.log('TX', command)

    def wait(self, prefix, start, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                events = [(seq, msg) for seq, msg in self.received if seq > start]
            for seq, msg in events:
                if msg.startswith(prefix):
                    return msg
                if msg.startswith(('AI:BLOCKED:', 'AI:ERROR:', 'ERROR:')):
                    raise RuntimeError(msg)
            time.sleep(.02)
        raise TimeoutError(f'Arduino did not confirm {prefix}')

    def exchange(self, command, prefix, timeout=4):
        start = self.rx_seq
        self.send(command)
        return self.wait(prefix, start, timeout)

    def receiver(self, link):
        while self.link is link and link.is_open:
            event = link.get_event(timeout=.2)
            if not event:
                continue
            msg = event.text
            with self.lock:
                self.rx_seq += 1
                self.received.append((self.rx_seq, msg))
                if msg == 'CAPS:PRESENTATION_V1':
                    self.state['firmware'] = True
                if msg == 'CAPS:ROUTE_V1_20CM':
                    self.state['route_firmware'] = True
                if msg == 'CAPS:MANUAL_ADVISORY_V1':
                    self.state['manual_firmware'] = True
                if msg == 'PRESENTATION:READY':
                    self.state['mode'] = 'PRESENTATION'
                if msg.startswith('STATUS:'):
                    fields = dict(item.split('=', 1) for item in msg[7:].split(',') if '=' in item)
                    self.state['mode'] = fields.get('MODE', 'UNKNOWN')
                    self.state['emergency'] = fields.get('EMERGENCY') == '1'
                if msg == 'EMERGENCY:STOPPED':
                    self.state.update(armed=False, auto=False, emergency=True, mode='IDLE')
                if msg in ('UP', 'DOWN', 'LEFT', 'RIGHT', 'OK', '1', '2', '3'):
                    self.state.update(armed=False, auto=False, mode='REMOTE')
                    self.epoch += 1
                if msg.startswith('STATUS:') and self.state['mode'] != 'PRESENTATION':
                    if self.state['armed']:
                        self.epoch += 1
                    self.state.update(armed=False, auto=False)
            if not msg.startswith(('IR code:', 'STATUS:')) and not (
                    self.vr_quiet and msg.startswith(('AI:DIST', 'AI:ACK:', 'AI:MOVE_DONE'))):
                self.log('RX', msg)

    def connect_serial(self, port):
        if not re.fullmatch(r'COM[1-9]\d*', port.upper()):
            raise ValueError('Enter a Windows COM port such as COM3.')
        if self.link:
            self.disconnect_serial()
        link = ArduinoSerial(port.upper())
        link.connect()
        self.link = link
        self.overlay = None
        with self.lock:
            self.state.update(serial_connected=True, source='live', firmware=False, route_firmware=False, manual_firmware=False,
                              armed=False, mode='CONNECTING', emergency=False, port=port.upper(),
                              scan=None, analysis=None, transport='usb')
            start = self.rx_seq
        threading.Thread(target=self.receiver, args=(link,), daemon=True).start()
        try:
            self.wait('CAPS:PRESENTATION_V1', start, 3)
            self.exchange('STATUS', 'STATUS:')
        except Exception:
            self.disconnect_serial()
            raise RuntimeError('Upload SmartCar_Presentation first, then connect again. Close Arduino Serial Monitor.')
        self.log('SYSTEM', 'Arduino connected. Select mode 4 to use the dashboard.')

    def verify_ble_firmware(self):
        start = self.rx_seq
        self.link.probe_route()
        self.wait('AI:ERROR:INVALID_ROUTE_PACKET', start, 5)
        self.state.update(firmware=True, route_firmware=True)

    def connect_ble(self):
        from arduino_ble import ArduinoBLE
        if self.link:
            self.disconnect_serial()
        link = ArduinoBLE()
        self.state.update(armed=False, auto=False, stage='Connecting to HC-08…')
        link.connect()
        self.link = link
        self.overlay = None
        self.state.update(serial_connected=True, source='live', transport='ble',
                          firmware=False, route_firmware=False, manual_firmware=False, port='HC-08 Bluetooth',
                          mode='CONNECTING', emergency=False, scan=None, analysis=None)
        threading.Thread(target=self.receiver, args=(link,), daemon=True).start()
        try:
            self.send('STOP')
            self.exchange('STATUS', 'STATUS:', 6)
            if not self.state['emergency']:
                self.verify_ble_firmware()
        except Exception as exc:
            self.disconnect_serial()
            raise RuntimeError('HC-08 connected, but Arduino verification failed: '+str(exc)) from exc
        self.state['stage'] = 'Bluetooth connected · controls OFF'
        self.log('SYSTEM', 'HC-08 wireless connection verified. Select mode 4, then Enable controls. If STOP is active, clear it first.')

    def disconnect_serial(self):
        self.epoch += 1
        self.state.update(armed=False, auto=False, serial_connected=False, firmware=False, route_firmware=False, manual_firmware=False, mode='DISCONNECTED', scan=None, analysis=None)
        self.overlay = None
        link, self.link = self.link, None
        if link:
            # Stop on explicit disconnect; never automatically resume an autonomous mode.
            link.disconnect(send_stop=True)

    def connect_camera(self, address, rotation):
        address = camera_address(address)
        rotation = int(rotation)
        if rotation not in (0, 90, 180, 270):
            raise ValueError('Invalid rotation.')
        self.disarm()
        if self.camera:
            self.camera.stop()
        self.camera = PhoneCamera(address).start()
        self.state.update(source='live', camera_url=address, rotation=rotation, scan=None, analysis=None)
        self.overlay = None
        if not self.camera.wait_until_ready(8):
            raise RuntimeError('Cannot reach IP Webcam. Start its server and connect phone and laptop to the same Wi-Fi/hotspot.')
        self.log('CAMERA', 'Live phone camera connected.')

    def picture(self):
        if self.state['source'] == 'replay':
            return Image.open(REPLAY).convert('RGB'), time.time()
        if not self.camera:
            raise RuntimeError('Connect the phone camera first.')
        raw, stamp = self.camera.get_latest_frame()
        if not raw or not stamp or time.time()-stamp > 2:
            raise RuntimeError('Camera frame is missing or stale. Movement blocked.')
        image = orient_image(Image.open(io.BytesIO(raw)).convert('RGB'), self.state['rotation'])
        crop = self.state['crop_bottom']
        if crop:
            # Cut off the car's own body (ultrasonic sensor) at the bottom of the frame.
            image = image.crop((0, 0, image.width, round(image.height * (100 - crop) / 100)))
        return image, stamp

    def live_boxes(self):
        """Camera frame with the floor-match zones drawn on it, for watching or recording."""
        picture, _ = self.picture()
        if picture.width > 720:
            picture = picture.resize((720, round(picture.height * 720 / picture.width)))
        scores = inspect_image(picture).clear_percent
        draw = ImageDraw.Draw(picture)
        width, height = picture.size
        line = max(3, width // 160)
        try:
            font = ImageFont.truetype('arial.ttf', max(14, width // 30))
        except OSError:
            font = ImageFont.load_default()
        # Same probe band that inspect_image compares against the near floor.
        y0, y1 = round(height * 150 / 240), round(height * 190 / 240)
        for index, zone in enumerate(('LEFT', 'CENTER', 'RIGHT')):
            x0, x1 = round(width * (index + 0.12) / 3), round(width * (index + 0.88) / 3)
            clear = scores[zone] >= PASS_IMAGE_CLEAR
            color = '#00e676' if clear else '#ff3b30'
            draw.rectangle((x0, y0, x1, y1), outline=color, width=line)
            label = f"{zone[0]} {scores[zone]}%"
            box = draw.textbbox((x0, y0), label, font=font)
            draw.rectangle((x0, y0 - (box[3]-box[1]) - 10, x0 + (box[2]-box[0]) + 10, y0), fill=color)
            draw.text((x0 + 5, y0 - (box[3]-box[1]) - 7), label, fill='black', font=font)
            draw.text((x0 + 5, y1 + 4), 'CLEAR' if clear else 'BLOCKED', fill=color, font=font)
        centre = (self.state.get('scan') or {}).get('centre')
        banner = 'IMAGE ANALYSIS · floor match' + (f' · sonar {centre} cm' if centre is not None else '')
        box = draw.textbbox((0, 0), banner, font=font)
        draw.rectangle((0, 0, width, (box[3]-box[1]) + 14), fill='black')
        draw.text((8, 6), banner, fill='white', font=font)
        return picture

    def mode(self):
        self.disarm()
        if self.state['source'] == 'replay':
            self.state.update(mode='REPLAY', emergency=False)
            return
        self.exchange('PRESENTATION', 'PRESENTATION:READY')
        self.state['mode'] = 'PRESENTATION'
        self.log('MODE', 'Mode 4 selected. Ready to arm.')

    def arm(self, manual=False):
        if self.state['emergency']:
            raise RuntimeError('Clear emergency stop first.')
        if not manual:
            self.picture()
        if self.state['source'] != 'replay':
            status = self.exchange('STATUS', 'STATUS:')
            if 'MODE=PRESENTATION,' not in status or 'EMERGENCY=1' in status:
                raise RuntimeError('Select mode 4 on the remote or dashboard first.')
            if manual and not self.state['manual_firmware']:
                start = self.rx_seq
                self.link.probe_manual()
                try:
                    self.wait('AI:ERROR:INVALID_MANUAL_PACKET', start, 4)
                except Exception as exc:
                    raise RuntimeError('Upload the new manual advisory firmware first. Manual warnings-only control is not available yet.') from exc
                self.state['manual_firmware'] = True
        self.epoch += 1
        self.state['armed'] = True
        self.state['manual_only'] = bool(manual)
        self.last_heartbeat = time.monotonic()
        self.state['warning'] = 'Manual mode: ultrasonic warnings do not prevent movement. Watch the car and keep its path clear.' if manual else ''
        self.log('ARM', 'Manual short steps ON; camera optional, ultrasonic is advisory only.' if manual else 'Short steps enabled by the operator.')

    def disarm(self):
        with self.send_lock:
            was_armed = self.state['armed']
            self.epoch += 1
            self.state.update(armed=False, auto=False, manual_only=False)
            if was_armed and self.link and self.state['mode'] == 'PRESENTATION':
                self.send('AI_STOP')

    def stop(self):
        with self.send_lock:
            self.epoch += 1
            self.state.update(armed=False, auto=False, emergency=True, stage='Stopped')
            if self.link:
                self.send('EMERGENCY_STOP')
        self.log('STOP', 'Emergency stop requested; pending actions cancelled.')

    def clear(self):
        if self.link:
            self.exchange('CLEAR_EMERGENCY', 'EMERGENCY:CLEARED')
            if self.state.get('transport') == 'ble' and not self.state['route_firmware']:
                self.verify_ble_firmware()
        self.state.update(emergency=False, armed=False, mode='IDLE' if self.link else 'REPLAY')
        self.log('SYSTEM', 'Stop cleared. Select mode 4 and arm again when ready.')

    def valid(self, epoch):
        if epoch != self.epoch or not self.state['armed'] or self.state['emergency']:
            raise RuntimeError('Action cancelled or controls disarmed.')
        if time.monotonic()-self.last_heartbeat > 3:
            raise RuntimeError('Dashboard heartbeat lost.')
        if self.state.get('manual_only'):
            if self.state['source'] != 'replay' and (not self.link or not self.link.is_open):
                raise RuntimeError('Car connection lost.')
        else:
            self.picture()

    def scan(self):
        if self.state['source'] == 'replay':
            return dict(left=68, centre=29, right=35)
        if not self.link or self.state['mode'] != 'PRESENTATION':
            return None
        self.exchange('AI_STOP', 'AI:ACK:S')
        result = parse_scan_event(self.exchange('AI_SCAN', 'AI:SCAN:', 4))
        if result is None:
            raise RuntimeError('Invalid ultrasonic response.')
        return result

    def analyze(self, execute=False, epoch=None):
        if execute and self.state.get('manual_only'):
            raise RuntimeError('Manual mode allows short direction commands only. Enable camera controls for image actions.')
        self.state.update(scan=None, analysis=None)
        self.overlay = None
        self.state['stage'] = 'Measuring clearance'
        scan = self.scan()
        self.log('SAMPLE' if self.state['source']=='replay' else 'SONAR',
                 str(scan) if scan else 'No live sensor scan: Arduino must be in mode 4.')
        self.state['stage'] = 'Capturing a fresh image'
        if self.camera and self.state['source'] == 'live':
            if not self.camera.wait_for_frame_after(time.time(), timeout=3):
                raise RuntimeError('A fresh camera image did not arrive after the scan.')
        picture, stamp = self.picture()
        self.log('FRAME', 'Recorded reference loaded.' if self.state['source']=='replay' else 'Fresh phone frame received after stopping.')
        self.state['stage'] = 'Analyzing image'
        preview = inspect_image(picture)
        direction, reason = decide(preview.clear_percent, scan)
        if self.state['source'] == 'replay':
            reason = reason.replace('measured clearance', 'sample clearance')
        self.log('IMAGE', 'Floor similarity: '+', '.join(f'{k.lower()} {v}%' for k,v in preview.clear_percent.items()))
        self.overlay = make_overlay(picture, preview)
        drawing = ImageDraw.Draw(self.overlay)
        drawing.rectangle((0, 0, self.overlay.width, 26), fill='black')
        drawing.text((5, 6), f'IMAGE HINT: {preview.suggestion} | command status in timeline', fill='white')
        self.state.update(scan=scan, analysis=dict(scores=preview.clear_percent,
                          direction=direction, reason=reason, time=time.strftime('%H:%M:%S'),
                          replay=self.state['source']=='replay'), stage='Decision ready')
        self.log('ANALYSIS', f'{direction}: {reason}')
        if execute:
            self.valid(epoch)
            if direction == 'STOP':
                self.state['auto'] = False
                self.log('BLOCK', 'Decision is STOP. No movement command sent.')
            else:
                self.move(direction, scan, epoch)

    def move(self, direction, scan, epoch):
        self.valid(epoch)
        if not scan or any(scan.get(k) is None for k in ('left', 'centre', 'right')):
            raise RuntimeError('Valid ultrasonic readings are required before movement.')
        if direction == 'FORWARD' and scan['centre'] <= 55:
            raise RuntimeError('Forward blocked: front clearance is 55 cm or less.')
        if direction in ('LEFT', 'RIGHT') and (scan[direction.lower()] <= 45 or scan['centre'] <= 25):
            raise RuntimeError('Turn blocked by ultrasonic clearance.')
        command, ack = {'LEFT': ('AI_LEFT_TEST', 'J'), 'RIGHT': ('AI_RIGHT_TEST', 'K'),
                        'FORWARD': ('AI_FORWARD', 'F'), 'BACKWARD': ('AI_BACKWARD', 'B')}[direction]
        self.state.update(stage='Sending short step', last_command=direction)
        if self.state['source'] == 'replay':
            self.log('SIMULATED', f'{direction} → simulated acknowledgement → simulated stop. No hardware used.')
            self.state['stage'] = 'Replay step complete'
            return
        status = self.exchange('STATUS', 'STATUS:')
        if 'MODE=PRESENTATION,' not in status or 'EMERGENCY=1' in status:
            self.disarm()
            raise RuntimeError('Remote changed mode; dashboard action cancelled.')
        with self.send_lock:
            self.valid(epoch)
            start = self.rx_seq
            self.send(command)
        self.wait('AI:ACK:'+ack, start, 1)
        self.state['stage'] = 'Arduino acknowledged'
        self.wait('AI:MOVE_DONE', start, 1.5)
        self.state['stage'] = 'Step complete · motors stopped'
        self.log('DONE', 'Arduino confirmed the pulse ended. Check the physical car to verify motion.')

    def voice(self, text, epoch):
        self.state['voice_text'] = text
        direction = voice_direction(text)
        self.log('VOICE', text)
        if not direction:
            raise ValueError('Use one clear command: forward, backward, left, right, or stop.')
        if direction == 'STOP':
            self.stop()
            return
        if not self.state['armed'] and not self.state['emergency']:
            raise ControlsOff('Controls are OFF, so no movement was sent. Select mode 4, Enable manual (no camera), then speak again.')
        self.valid(epoch)
        if self.state.get('manual_only'):
            self.manual_move(direction, epoch)
            return
        scan = self.scan()
        self.state['scan'] = scan
        self.move(direction, scan, epoch)

    def manual_move(self, direction, epoch):
        self.valid(epoch)
        replay = self.state['source'] == 'replay'
        if not replay and not self.state['manual_firmware']:
            raise RuntimeError('New manual advisory firmware is required.')
        if replay:
            self.state.update(last_command=direction, stage='Simulated manual step complete')
            self.log('SIMULATED', direction+' manual pulse; no hardware used.')
            return
        self.manual_advisory(direction)
        duration = 120 if direction in ('LEFT','RIGHT') else 350
        self.manual_pulse(direction, duration, epoch)
        self.state['stage'] = 'Manual step complete · controls stay ON'
        self.log('DONE','Manual pulse ended. Controls stay ON for your next direction.')

    def manual_advisory(self, direction):
        # One centred advisory reading only. Never sweeps left/right or treats
        # an absent/near echo as permission failure in operator-controlled mode.
        try:
            distance_line = self.exchange('AI_DISTANCE', 'AI:DIST', 1)
        except TimeoutError:
            distance_line = ''
        match = re.fullmatch(r'AI:DIST_CM=(\d+)', distance_line)
        distance = int(match[1]) if match else None
        self.state['scan'] = {'left':None, 'centre':distance, 'right':None}
        warning = ''
        if distance is None:
            warning = 'Ultrasonic reading unavailable. Following your manual command; check clearance yourself.'
        elif distance <= 20:
            warning = f'Object measured {distance} cm ahead. Following your manual command; check clearance yourself.'
        elif direction in ('LEFT','RIGHT','BACKWARD'):
            warning = 'Side/rear clearance is not measured. Following your manual command.'
        self.state['warning'] = warning or 'Manual mode: ultrasonic is advisory only. Watch the car.'
        if warning:self.log('WARN', warning)
    def manual_pulse(self, direction, duration, epoch):
        status = self.exchange('STATUS', 'STATUS:')
        if 'MODE=PRESENTATION,' not in status or 'EMERGENCY=1' in status:
            raise RuntimeError('Remote changed mode; manual command cancelled.')
        with self.send_lock:
            self.valid(epoch)
            start = self.rx_seq
            self.link.send_manual_pulse(direction, duration)
        self.state.update(last_command=direction, stage='Manual short step sent')
        self.log('TX', f'MANUAL {direction} {duration} ms · sensor warnings only')
        self.wait('AI:ACK:'+{'FORWARD':'F','BACKWARD':'B','LEFT':'L','RIGHT':'R'}[direction], start, 2)
        self.wait('AI:MOVE_DONE', start, 2)

    def preview_route(self, text):
        self.state.update(route_plan=None, route_text='', route_status='Parsing route')
        plan = route_plan(text, manual=self.state.get('manual_only',False))
        summary = ' → '.join(f"{s['direction'].lower()} {s['value']} {s['unit']}" for s in plan) + ' → stop'
        self.state.update(route_plan=plan, route_text=text, route_status='Approximate route: '+summary)
        self.log('ROUTE', 'Preview only · '+summary)

    def run_route(self, text, epoch):
        if self.state.get('manual_only'):
            return self.run_manual_route(text, epoch)
        plan = route_plan(text)
        if text != self.state['route_text'] or plan != self.state['route_plan']:
            raise RuntimeError('Preview this exact route before running it.')
        self.valid(epoch)
        replay = self.state['source']=='replay'
        if not replay and not self.state['route_firmware']:
            raise RuntimeError('Upload the new route-capable sketch first. Existing controls still work.')
        deadline = time.monotonic()+45
        self.log('ROUTE', 'Starting approximate route · 20 cm stop gate · no exact distance feedback')
        for index, step in enumerate(plan, 1):
            self.valid(epoch)
            self.state['route_status'] = f"Step {index}/{len(plan)}: {step['direction'].lower()} approximately {step['value']} {step['unit']}"
            scan = self.scan()
            self.state['scan'] = scan
            route_clearance(step['direction'], scan)
            for pulse_index, duration in enumerate(route_pulses(step)):
                self.valid(epoch)
                if time.monotonic() > deadline: raise TimeoutError('Route time limit reached.')
                if step['direction'] != 'FORWARD' and pulse_index:
                    scan = self.scan()
                    self.state['scan'] = scan
                    route_clearance(step['direction'], scan)
                if replay:
                    self.log('SIMULATED', f"Route {step['direction']} {duration} ms · no hardware")
                    continue
                status = self.exchange('STATUS', 'STATUS:')
                if 'MODE=PRESENTATION,' not in status or 'EMERGENCY=1' in status:
                    raise RuntimeError('Mode changed; route cancelled.')
                with self.send_lock:
                    self.valid(epoch)
                    start = self.rx_seq
                    self.link.send_route_pulse(step['direction'], duration)
                    self.state['last_command'] = 'ROUTE '+step['direction']
                    self.log('TX', f"ROUTE {step['direction']} {duration} ms · gate 20 cm")
                code = {'FORWARD':'F','LEFT':'L','RIGHT':'R'}[step['direction']]
                self.wait('AI:ACK:'+code, start, 1)
                self.wait('AI:MOVE_DONE', start, 1.5)
                self.valid(epoch)
                # STOP/remote takeover is rechecked before every subsequent pulse.
        self.state.update(route_status='Route pulse sequence finished · verify physical position', stage='Route complete · stopped')
        self.log('DONE', 'Route pulses finished; distances and angles are estimates, not measured positions.')
        self.disarm()

    def run_manual_route(self, text, epoch):
        plan = route_plan(text, manual=True)
        if text != self.state['route_text'] or plan != self.state['route_plan']:
            raise ValueError('Preview this exact route before running it.')
        self.valid(epoch)
        replay = self.state['source'] == 'replay'
        if not replay and not self.state['manual_firmware']:
            raise RuntimeError('Upload the new manual advisory firmware first.')
        self.log('ROUTE','Manual route starting · camera optional · sensor warnings only')
        for index, step in enumerate(plan,1):
            self.valid(epoch)
            self.state['route_status'] = f"Manual {index}/{len(plan)}: {step['direction'].lower()} approximately {step['value']} {step['unit']}"
            if not replay:self.manual_advisory(step['direction'])
            for duration in route_pulses(step):
                self.valid(epoch)
                if replay:
                    self.log('SIMULATED',f"Manual route {step['direction']} {duration} ms · no hardware")
                else:
                    self.manual_pulse(step['direction'],duration,epoch)
        self.state.update(route_status='Manual route finished · stopped · controls stay ON',stage='Manual route complete · controls stay ON')
        self.log('DONE','Manual route finished. Distances/angles are estimates. Controls remain ON.')

    def ensure_manual_firmware(self):
        if self.state['manual_firmware']:
            return
        start = self.rx_seq
        self.link.probe_manual()
        try:
            self.wait('AI:ERROR:INVALID_MANUAL_PACKET', start, 4)
        except Exception as exc:
            raise RuntimeError('Pass mode needs the manual advisory firmware (SmartCar_Presentation).') from exc
        self.state['manual_firmware'] = True

    def pass_look(self):
        """Stationary check: one centre ultrasonic reading plus a fresh camera frame."""
        distance = None
        for _ in range(2):
            try:
                line = self.exchange('AI_DISTANCE', 'AI:DIST', 1)
            except TimeoutError:
                line = ''
            match = re.fullmatch(r'AI:DIST_CM=(\d+)', line)
            if match:
                distance = int(match[1])
                break
        time.sleep(.25)  # let the phone frame settle after the car stopped
        if not self.camera.wait_for_frame_after(time.time(), timeout=3):
            raise RuntimeError('A fresh camera image did not arrive; pass mode stopped.')
        picture, _ = self.picture()
        preview = inspect_image(picture)
        scores = preview.clear_percent
        self.overlay = make_overlay(picture, preview)
        self.state.update(scan={'left': None, 'centre': distance, 'right': None},
                          analysis=dict(scores=scores, direction='PASS', time=time.strftime('%H:%M:%S'),
                                        reason='Pass mode check', replay=False))
        return distance, scores

    def run_pass(self, text, epoch, turn_deg=PASS_TURN_DEG, safety_cm=PASS_SAFETY_CM):
        target = pass_distance(text)
        turn_deg, safety_cm = int(turn_deg), int(safety_cm)
        if not 10 <= turn_deg <= 25 or not 15 <= safety_cm <= 80:
            raise ValueError('Turn step must be 10–25° (shorter pulses barely move the car) and ultrasonic limit 15–80 cm.')
        if not self.state['armed'] and not self.state['emergency']:
            raise ControlsOff('Controls are OFF, so pass mode did not start. Select mode 4 and enable controls first.')
        self.valid(epoch)
        if self.state['source'] == 'replay' or not self.link:
            raise RuntimeError('Pass mode needs the live car connected.')
        if not self.camera or not self.state.get('camera_url'):
            raise RuntimeError('Pass mode needs the phone camera. Connect IP Webcam first.')
        self.ensure_manual_firmware()
        x = y = 0.0
        heading = 0
        avoid_side, avoid_until_x = None, 0.0
        points, obstacles = [[0, 0]], []
        started = time.monotonic()
        self.log('PASS', f'Pass mode: reach the point {target} cm ahead. Blocked = ultrasonic ≤{safety_cm} cm '
                         f'or image centre <{PASS_IMAGE_CLEAR}%. Turns {turn_deg}° per check.')

        def publish(message):
            self.state['pass_status'] = (f'{message} · {round(x)}/{target} cm ahead, '
                                         f'{abs(round(y))} cm {"right" if y >= 0 else "left"} of line · heading {heading:+d}°')
            self.state['pass_track'] = dict(target=target, heading=heading,
                                            points=[p[:] for p in points], obstacles=[o[:] for o in obstacles])

        def turn(side):
            nonlocal heading
            self.manual_pulse(side, turn_deg * 12, epoch)
            heading += turn_deg if side == 'RIGHT' else -turn_deg

        publish('Starting')
        for _ in range(PASS_MAX_CYCLES):
            self.valid(epoch)
            if time.monotonic() - started > PASS_MAX_SECONDS:
                raise TimeoutError('Pass mode time limit reached.')
            remaining = target - x
            if remaining <= PASS_GOAL_TOLERANCE_CM:
                break
            publish('Checking')
            distance, scores = self.pass_look()
            reason = pass_blocked(distance, scores, safety_cm)
            if not reason and distance is not None and distance - safety_cm < PASS_MIN_STEP_CM:
                reason = f'ultrasonic {distance} cm leaves no room for a step'
            seen = f"US {distance if distance is not None else 'no echo'} cm · L{scores['LEFT']} C{scores['CENTER']} R{scores['RIGHT']}%"
            if reason:
                ahead = distance if distance is not None and distance < safety_cm + PASS_MIN_STEP_CM else PASS_IMAGE_OBSTACLE_CM
                obstacle = [round(x + ahead * math.cos(math.radians(heading)), 1),
                            round(y + ahead * math.sin(math.radians(heading)), 1)]
                obstacles.append(obstacle)
                # Stay on the avoiding side until the car is past this obstacle along the line.
                avoid_until_x = max(avoid_until_x, obstacle[0] + PASS_CLEAR_MARGIN_CM)
                side = pass_turn_side(avoid_side, scores)
                if abs(heading + (turn_deg if side == 'RIGHT' else -turn_deg)) > PASS_MAX_HEADING:
                    publish('No clear way found, stopped')
                    self.log('PASS', f'Blocked ({reason}) at heading {heading:+d}°. Stopped; no clear path found.')
                    return
                self.log('PASS', f'Blocked: {reason} [{seen}] → turn {side.lower()} {turn_deg}°, then check again')
                publish(f'Blocked, turning {side.lower()}')
                turn(side)
                avoid_side = side
                continue
            if avoid_side and x >= avoid_until_x:
                avoid_side = None  # past the obstacle; head back toward the destination
                self.log('PASS', f'Past the obstacle ({round(x)} cm ahead) → heading back toward the line')
            goal = pass_goal_heading(x, y, target)
            if not avoid_side and abs(goal - heading) >= turn_deg * 0.75:
                side = 'RIGHT' if goal > heading else 'LEFT'
                self.log('PASS', f'Clear [{seen}] → turn {side.lower()} {turn_deg}° toward the destination '
                                 f'(aim {goal:+.0f}°), then check again')
                publish(f'Steering {side.lower()} toward destination')
                turn(side)
                continue
            rad = math.radians(heading)
            step = min(PASS_STEP_CM, max(PASS_MIN_STEP_CM, round(remaining / max(math.cos(rad), .3))))
            if distance is not None:
                step = min(step, distance - safety_cm)  # never drive inside the ultrasonic limit
            self.log('PASS', f'Clear [{seen}] → forward {step} cm')
            publish('Moving forward')
            for duration in route_pulses(dict(value=step, unit='cm')):
                self.manual_pulse('FORWARD', duration, epoch)
            x += step * math.cos(rad)
            y += step * math.sin(rad)
            points.append([round(x, 1), round(y, 1)])
        else:
            publish('Step limit reached, stopped')
            self.log('PASS', 'Step limit reached. Stopped.')
            return
        while abs(heading) >= turn_deg:
            turn('LEFT' if heading > 0 else 'RIGHT')
        publish('Destination reached, stopped')
        self.log('DONE', f'Pass mode finished about {round(x)} cm ahead and {abs(round(y))} cm '
                         f'{"right" if y >= 0 else "left"} of the line, facing the original direction. '
                         'Position is estimated from timing.')

    def request_voice(self, text):
        if voice_direction(text) is None:
            raise ValueError('Use one command: forward, backward, left, right, or stop.')
        if not self.state['busy']:
            return self.job('voice', {'text':text})
        if self.state['operation'] not in ('auto', 'analyze', 'step', 'route_run', 'pass_run'):
            raise RuntimeError('Wait for the connection/setup operation to finish.')
        with self.send_lock:
            self.valid(self.epoch)
            self.epoch += 1
            epoch = self.epoch
            self.state['auto'] = False
            if self.link and self.state['mode'] == 'PRESENTATION':
                self.send('AI_STOP')
        self.log('OVERRIDE', 'Voice/typed command cancelled the camera sequence.')
        def pending():
            deadline = time.monotonic()+6
            while time.monotonic()<deadline:
                if epoch != self.epoch: return
                if not self.state['busy']:
                    try: self.job('voice', {'text':text})
                    except Exception as exc: self.log('ERROR', exc)
                    return
                time.sleep(.05)
            self.log('ERROR', 'Voice override timed out without sending movement.')
        threading.Thread(target=pending, daemon=True).start()

    def job(self, action, data):
        if not self.operation.acquire(False):
            raise RuntimeError('One action is already running. STOP is always available.')
        epoch = self.epoch
        self.state.update(busy=True, error='', operation=action)
        def run():
            try:
                if action == 'camera': self.connect_camera(data.get('url', ''), data.get('rotation', 180))
                elif action == 'connect': self.connect_serial(data.get('port', 'COM3'))
                elif action == 'connect_ble': self.connect_ble()
                elif action == 'disconnect': self.disconnect_serial()
                elif action == 'mode': self.mode()
                elif action == 'arm': self.arm(manual=data.get('manual') is True)
                elif action == 'clear': self.clear()
                elif action == 'analyze': self.analyze()
                elif action == 'step': self.analyze(True, epoch)
                elif action == 'voice': self.voice(str(data.get('text', '')), epoch)
                elif action == 'route_preview': self.preview_route(str(data.get('text', '')))
                elif action == 'route_run': self.run_route(str(data.get('text', '')), epoch)
                elif action == 'vr': self.vr.run()
                elif action == 'pass_run': self.run_pass(str(data.get('text', '')), epoch,
                                                         data.get('turn_deg', PASS_TURN_DEG), data.get('safety_cm', PASS_SAFETY_CM))
                elif action == 'auto':
                    self.valid(epoch)
                    self.state['auto'] = True
                    for _ in range(5):
                        if not self.state['auto']: break
                        self.analyze(True, epoch)
                        if self.state['auto']: time.sleep(1)
                    self.state['auto'] = False
                    self.log('SYSTEM', 'Five-step limit reached or loop stopped. Start again deliberately if needed.')
                elif action == 'replay':
                    if self.link: raise RuntimeError('Disconnect Arduino before switching to recorded demo.')
                    self.disarm()
                    self.state.update(source='replay', mode='REPLAY', emergency=False, analysis=None, scan=None)
                    self.overlay = None
                else: raise ValueError('Unknown action.')
            except Exception as exc:
                if epoch != self.epoch and action in ('step','auto','voice','route_run','pass_run'):
                    self.log('CANCEL', 'Pending action cancelled by a newer control input.')
                    return
                self.state.update(error=str(exc), stage='Action blocked', auto=False)
                self.log('ERROR', exc)
                if isinstance(exc, ControlsOff):
                    return
                if action == 'route_run': self.state['route_status'] = 'Route stopped: '+str(exc)
                if action == 'pass_run':
                    self.state['pass_status'] = 'Stopped: '+str(exc)
                    if isinstance(exc, ValueError):
                        return
                    try: self.stop()
                    except Exception: pass
                    return
                if action in ('voice','route_run') and self.state.get('manual_only'):
                    if isinstance(exc, ValueError):
                        self.state['warning'] = str(exc)+' Controls remain ON; no movement sent.'
                    else:
                        try:self.disarm()
                        except Exception:pass
                elif action in ('step', 'auto', 'voice', 'route_run'):
                    try: self.stop()
                    except Exception: pass
            finally:
                self.state.update(busy=False, operation='')
                self.operation.release()
        threading.Thread(target=run, daemon=True).start()

    def watchdog(self):
        while True:
            time.sleep(.4)
            if self.state['armed']:
                try:
                    self.valid(self.epoch)
                    if self.link and not self.link.is_open:
                        raise RuntimeError('Serial disconnected.')
                except Exception as exc:
                    self.log('WATCHDOG', str(exc))
                    try: self.stop()
                    except Exception: pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def respond(self, code, data, content='application/json'):
        if isinstance(data, (dict, list)): data = json.dumps(data).encode()
        elif isinstance(data, str): data = data.encode()
        self.send_response(code)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def local_request(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.local_request(): return self.respond(403, {'error':'Local access only'})
        path = urlsplit(self.path).path
        if path == '/api/state': return self.respond(200, app.snapshot())
        if path in ('/frame.jpg', '/analysis.jpg'):
            try:
                if path == '/analysis.jpg' and app.overlay:
                    pic = app.overlay
                elif path == '/frame.jpg' and 'plain=1' in urlsplit(self.path).query:
                    pic = app.picture()[0]
                    if pic.width > 720:
                        pic = pic.resize((720, round(pic.height * 720 / pic.width)))
                elif path == '/frame.jpg' and app.state['boxes'] and app.state['source'] == 'live':
                    pic = app.live_boxes()
                else:
                    pic = app.picture()[0]
                buffer = io.BytesIO()
                pic.save(buffer, format='JPEG', quality=85)
                return self.respond(200, buffer.getvalue(), 'image/jpeg')
            except Exception as exc: return self.respond(503, {'error':str(exc)})
        files = {'/':('index.html','text/html; charset=utf-8'), '/app.js':('app.js','text/javascript'),
                 '/style.css':('style.css','text/css'), '/vr':('vr.html','text/html; charset=utf-8'),
                 '/vr.js':('vr.js','text/javascript'), '/vr.css':('vr.css','text/css')}
        if path in files:
            name, mime = files[path]
            data = (ROOT/'static'/name).read_text(encoding='utf-8').replace('__SESSION_TOKEN__', TOKEN)
            return self.respond(200, data, mime)
        self.respond(404, {'error':'Not found'})

    def do_POST(self):
        if not self.local_request() or self.headers.get('X-Session') != TOKEN:
            return self.respond(403, {'error':'Reload the local dashboard.'})
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
            return self.respond(403, {'error':'Origin refused'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 4096: raise ValueError('Request too large')
            data = json.loads(self.rfile.read(size) or b'{}')
            action = self.path.removeprefix('/api/')
            if action == 'heartbeat':
                app.last_heartbeat = time.monotonic()
                if data.get('vr') is True: app.vr.beat = time.monotonic()
            elif action == 'vr_command': app.vr.command(str(data.get('text', '')))
            elif action == 'vr_hold': app.vr.hold()
            elif action == 'vr_engine': app.vr.request_engine()
            elif action == 'vr_exit': app.vr.exit()
            elif action == 'vr_reset_path': app.vr.reset_path()
            elif action == 'stop': app.stop()
            elif action == 'disarm': app.disarm()
            elif action == 'view':
                crop = int(data.get('crop_bottom', app.state['crop_bottom']))
                if not 0 <= crop <= 40: raise ValueError('Crop must be 0–40%.')
                app.state.update(crop_bottom=crop, boxes=data.get('boxes', app.state['boxes']) is True)
            elif action == 'voice' and voice_direction(str(data.get('text',''))) == 'STOP': app.stop()
            elif action == 'voice': app.request_voice(str(data.get('text','')))
            else: app.job(action, data)
            self.respond(202, {'ok':True})
        except Exception as exc: self.respond(400, {'error':str(exc)})


if __name__ == '__main__':
    import faulthandler
    # If native code ever crashes the server, record where (Python cannot show a normal traceback then).
    crash_log = open(ROOT / 'server-crash.log', 'a', encoding='utf-8')
    crash_log.write(f'--- server started {time.strftime("%Y-%m-%d %H:%M:%S")} ---\n'); crash_log.flush()
    faulthandler.enable(file=crash_log, all_threads=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    app = Dashboard()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'SmartCar dashboard: http://127.0.0.1:{args.port}', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        app.disconnect_serial()
        if app.camera: app.camera.stop()
        server.server_close()
