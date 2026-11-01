"""VR View missions: teach-and-repeat, go home, go to an object, follow me, "what do you see".

Kept separate from vr.py; VRController mixes this in. Missions only use the same bounded
pulses and autopilot legs as the rest of VR View, so every existing safety layer still applies
(hold, E-stop, heartbeat, remote takeover, engine off).
Route and home driving use the autopilot for each leg, so obstacles are still avoided.
Object finding and following use the pre-trained detector in detector.py.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import detector

ROUTE_FILE = Path(__file__).resolve().parent / 'routes' / 'taught_route.json'
ROUTE_SIMPLIFY_CM = 5      # path simplification tolerance (keeps the corners)
ROUTE_MIN_LEG_CM = 5
TURN_TOLERANCE_DEG = 5
HFOV_DEG = 50              # approximate horizontal view of the phone camera
CENTER_TOLERANCE = 0.15    # target counts as centred within ±15% of half the picture
SEARCH_TURN_DEG = 30
GOTO_STOP_CM = 25          # stop this far from the object (ultrasonic)
GOTO_ARRIVE_BOX = 0.75     # or when the object fills this much of the picture height
GOTO_STEP_CM = 15
FOLLOW_TURN_TOLERANCE = 0.2
FOLLOW_NEAR_CM = 50        # closer than this: wait
FOLLOW_FAR_CM = 90         # further than this: move closer
FOLLOW_BOX_FAR = 0.45      # without an echo, a person box shorter than this is "far"
FOLLOW_LOST_LIMIT = 8      # checks without seeing the person before "I lost you"
DETECTIONS_SHOW_S = 2.5


def angle_diff(target, current):
    return (target - current + 180) % 360 - 180


def simplify(points, tolerance=ROUTE_SIMPLIFY_CM):
    """Ramer-Douglas-Peucker: drop points that lie almost on a straight line, keep the corners."""
    if len(points) < 3:
        return [p[:] for p in points]
    (x0, y0), (x1, y1) = points[0], points[-1]
    length = math.hypot(x1 - x0, y1 - y0) or 1e-9
    worst, index = 0.0, 0
    for i, (x, y) in enumerate(points[1:-1], 1):
        d = abs((y1 - y0) * x - (x1 - x0) * y + x1 * y0 - y1 * x0) / length
        if d > worst:
            worst, index = d, i
    if worst <= tolerance:
        return [points[0][:], points[-1][:]]
    return simplify(points[:index + 1], tolerance)[:-1] + simplify(points[index:], tolerance)


def to_local(point, origin, heading):
    """World (east, north) -> (along, right) relative to a start pose."""
    t = math.radians(heading)
    dx, dy = point[0] - origin[0], point[1] - origin[1]
    return [round(dx * math.sin(t) + dy * math.cos(t), 1), round(dx * math.cos(t) - dy * math.sin(t), 1)]


def to_world(local, origin, heading):
    t = math.radians(heading)
    along, right = local
    return [round(origin[0] + along * math.sin(t) + right * math.cos(t), 1),
            round(origin[1] + along * math.cos(t) - right * math.sin(t), 1)]


def side_of(box):
    centre = (box[0] + box[2]) / 2
    return 'on the left' if centre < 0.38 else 'on the right' if centre > 0.62 else 'ahead'


def describe_scene(detections, sonar):
    if not detections:
        return 'I do not see any objects I know.'
    parts, seen = [], set()
    for det in detections:
        key = (det['label'], side_of(det['box']))
        if key in seen:
            continue
        seen.add(key)
        text = f"a {det['label']} {key[1]}"
        if key[1] == 'ahead' and sonar is not None:
            text += f', about {sonar} cm away'
        parts.append(text)
        if len(parts) == 5:
            break
    return 'I see ' + ', '.join(parts[:-1]) + (' and ' if len(parts) > 1 else '') + parts[-1] + '.'


class MissionMixin:
    # ---------- state ----------
    def mission_init(self):
        self.mission = None
        self.recording = None
        self.detections = []
        self.detections_at = 0.0
        self.speech = None
        self.speech_id = 0
        self.taught = None
        try:
            self.taught = json.loads(ROUTE_FILE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass

    def mission_view(self):
        m = self.mission
        view = dict(recording=bool(self.recording), taught=bool(self.taught),
                    taught_cm=self.taught.get('length') if self.taught else None, speech=self.speech,
                    detections=self.detections if time.time() - self.detections_at < DETECTIONS_SHOW_S else [])
        if m:
            view.update(kind=m['kind'], status=m['status'], label=m.get('label'),
                        waypoints=[w[:] for w in m.get('waypoints', [])], index=m.get('index', 0))
        return view

    def speak(self, text):
        self.speech_id += 1
        self.speech = dict(id=self.speech_id, text=text)
        self.d.log('VR', 'Car says: ' + text)

    def end_mission(self, status, speak=None):
        if self.mission:
            self.d.log('VR', f"Mission {self.mission['kind']}: {status}")
            self.mission = None
        self.doing = 'HOLD'
        self.say(status)
        if speak:
            self.speak(speak)

    # ---------- teach and repeat ----------
    def start_recording(self):
        self.recording = dict(start=len(self.points) - 1, origin=[self.east, self.north], heading=self.heading)
        self.say('Recording your route. Drive it, then say "stop recording".')
        self.d.log('VR', 'Route recording started.')

    def stop_recording(self):
        r = self.recording
        if not r:
            self.say('Not recording. Say "record route" first.')
            return
        self.recording = None
        path = [p[:] for p in self.points[r['start']:]] + [[round(self.east, 1), round(self.north, 1)]]
        local = simplify([to_local(p, r['origin'], r['heading']) for p in path])
        length = round(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(local, local[1:])))
        if length < ROUTE_MIN_LEG_CM:
            self.say('Route too short to save. Drive a bit further while recording.')
            return
        self.taught = dict(points=local, final=round(angle_diff(self.heading, r['heading']), 1), length=length,
                           saved=time.strftime('%Y-%m-%d %H:%M'))
        try:
            ROUTE_FILE.parent.mkdir(exist_ok=True)
            ROUTE_FILE.write_text(json.dumps(self.taught, indent=1), encoding='utf-8')
        except OSError:
            pass
        self.say(f'Route saved: about {length} cm, {len(local) - 1} legs. Put the car back at the start and say "repeat route".')
        self.d.log('VR', f'Route saved ({length} cm, {len(local) - 1} legs).')

    def start_repeat(self):
        if not self.taught:
            self.say('No route saved yet. Say "record route", drive, then "stop recording".')
            return
        origin, heading = [self.east, self.north], self.heading
        waypoints = [to_world(p, origin, heading) for p in self.taught['points'][1:]]
        self.mission = dict(kind='route', label='taught route', waypoints=waypoints, index=0,
                            final=heading + self.taught['final'], leg=None, status='Starting route')
        self.d.log('VR', f'Repeating taught route from here ({len(waypoints)} legs).')

    def start_home(self):
        path = [p[:] for p in self.points] + [[round(self.east, 1), round(self.north, 1)]]
        if math.hypot(self.east, self.north) < ROUTE_MIN_LEG_CM:
            self.say('Already at home.')
            return
        back = simplify(list(reversed(path)))[1:]
        self.mission = dict(kind='home', label='home', waypoints=back, index=0, final=0.0, leg=None,
                            status='Going home along the path I came')
        self.d.log('VR', f'Going home: retracing {len(back)} legs.')

    def route_step(self):
        m = self.mission
        leg = m.get('leg')
        if leg and leg['finished'] and not leg['status'].startswith('Destination'):
            self.end_mission(f"{m['label'].capitalize()} stopped: {leg['status']}")
            return
        if m['index'] >= len(m['waypoints']):
            m['status'] = 'Turning to the final direction'
            self.turn_to(m['final'])
            done = 'Home' if m['kind'] == 'home' else 'Route complete'
            self.end_mission(f'{done}. Position is estimated from timing.', speak=f'{done}.')
            return
        target = m['waypoints'][m['index']]
        dx, dy = target[0] - self.east, target[1] - self.north
        distance = math.hypot(dx, dy)
        m['index'] += 1
        if distance < ROUTE_MIN_LEG_CM:
            return
        m['status'] = f"Leg {m['index']}/{len(m['waypoints'])}: {round(distance)} cm"
        self.doing = 'ROUTE · TURNING' if m['kind'] == 'route' else 'GOING HOME · TURNING'
        self.publish()
        self.turn_to(math.degrees(math.atan2(dx, dy)))
        self.start_auto(round(distance), final=None, quiet=True)
        m['leg'] = self.auto

    def turn_to(self, target):
        from vr import MS_PER_DEG, TURN_PULSE_MS
        seq = self.cancel
        diff = angle_diff(target, self.heading)
        while abs(diff) >= TURN_TOLERANCE_DEG and seq == self.cancel and self.engine:
            self.pulse('RIGHT' if diff > 0 else 'LEFT', round(min(TURN_PULSE_MS, abs(diff) * MS_PER_DEG)))
            diff = angle_diff(target, self.heading)
            self.publish()

    # ---------- camera helpers ----------
    def look_objects(self):
        """Stop-and-look: fresh frame, object detection, one ultrasonic reading."""
        if self.d.camera:
            time.sleep(0.25)
            try:
                self.d.camera.wait_for_frame_after(time.time(), timeout=2)
            except Exception:
                pass
        picture = self.d.picture()[0]
        self.detections = detector.detect(picture)
        self.detections_at = time.time()
        if self.d.state['mode'] == 'PRESENTATION':
            self.sonar = self.read_sonar()
        return self.detections

    def forward_step(self, cm):
        from vr import MS_PER_CM, pulse_plan
        seq, remaining = self.cancel, round(cm * MS_PER_CM)
        while remaining > 0 and seq == self.cancel and self.engine and self.mission:
            pulse, pause = pulse_plan(self.level)
            chunk = min(pulse, remaining)
            self.pulse('FORWARD', chunk)
            remaining -= chunk
            self.publish()
            if remaining > 0:
                self.pause(pause / 1000)

    def aim(self, box, tolerance):
        """Turn toward a detection box if it is off-centre. Returns True if a turn was made."""
        from vr import MS_PER_DEG
        offset = ((box[0] + box[2]) / 2 - 0.5) * 2   # -1 = left edge, +1 = right edge
        if abs(offset) <= tolerance:
            return False
        degrees = max(10, min(30, round(abs(offset) * HFOV_DEG / 2)))
        self.pulse('RIGHT' if offset > 0 else 'LEFT', degrees * MS_PER_DEG)
        return True

    # ---------- what do you see ----------
    def see(self):
        self.doing = 'LOOKING'
        self.publish()
        try:
            detections = self.look_objects()
        except RuntimeError as exc:
            self.say(str(exc))
            self.speak('My object detection is not installed.')
            return
        text = describe_scene(detections, self.sonar)
        self.report = dict(lines=[f"{d['label']} {round(d['conf'] * 100)}% {side_of(d['box'])}" for d in detections[:8]]
                           or ['No known objects in view'], advice=text, safest='', time=time.strftime('%H:%M:%S'))
        self.speak(text)
        self.doing = 'HOLD'

    # ---------- go to an object ----------
    def start_goto(self, label):
        self.mission = dict(kind='goto', label=label, searched=0, side='RIGHT', status=f'Looking for the {label}')
        self.d.log('VR', f'Go to the {label}: searching.')

    def goto_step(self):
        m = self.mission
        self.doing = f"GO TO {m['label'].upper()}"
        self.publish()
        try:
            detections = self.look_objects()
        except RuntimeError as exc:
            self.end_mission(str(exc), speak='My object detection is not installed.')
            return
        targets = [d for d in detections if d['label'] == m['label']]
        if not targets:
            if m['searched'] >= 360:
                self.end_mission(f"I could not find a {m['label']}.", speak=f"I could not find a {m['label']}.")
                return
            from vr import MS_PER_DEG
            m['status'] = f"Searching for the {m['label']} ({m['searched']}°)"
            self.pulse(m['side'], SEARCH_TURN_DEG * MS_PER_DEG)
            m['searched'] += SEARCH_TURN_DEG
            return
        box = max(targets, key=lambda d: d['conf'])['box']
        m['searched'] = 0
        if abs((box[0] + box[2]) / 2 - 0.5) > 0.05:
            m['side'] = 'RIGHT' if (box[0] + box[2]) / 2 > 0.5 else 'LEFT'
        if self.aim(box, CENTER_TOLERANCE):
            m['status'] = f"Turning toward the {m['label']}"
            return
        if (self.sonar is not None and self.sonar <= GOTO_STOP_CM) or box[3] - box[1] >= GOTO_ARRIVE_BOX:
            self.end_mission(f"Reached the {m['label']}.", speak=f"I reached the {m['label']}.")
            return
        step = GOTO_STEP_CM if self.sonar is None else max(3, min(GOTO_STEP_CM, self.sonar - GOTO_STOP_CM))
        m['status'] = f"Approaching the {m['label']}" + (f" · {self.sonar} cm" if self.sonar is not None else '')
        self.forward_step(step)

    # ---------- follow me ----------
    def start_follow(self):
        self.mission = dict(kind='follow', label='person', lost=0, side='RIGHT', told=False, status='Looking for you')
        self.d.log('VR', 'Follow me: started.')

    def follow_step(self):
        m = self.mission
        self.doing = 'FOLLOW ME'
        self.publish()
        try:
            detections = self.look_objects()
        except RuntimeError as exc:
            self.end_mission(str(exc), speak='My object detection is not installed.')
            return
        people = [d for d in detections if d['label'] == 'person']
        if not people:
            m['lost'] += 1
            if m['lost'] > FOLLOW_LOST_LIMIT:
                m['status'] = 'Lost you · waiting. Step in front of the camera'
                if not m['told']:
                    m['told'] = True
                    self.speak('I lost you. Please step in front of me.')
                self.pause(0.6)
            elif m['lost'] >= 2:
                from vr import MS_PER_DEG
                m['status'] = 'Looking for you'
                self.pulse(m['side'], 20 * MS_PER_DEG)
            return
        m.update(lost=0, told=False)
        box = max(people, key=lambda d: d['box'][3] - d['box'][1])['box']   # nearest = tallest
        centre = (box[0] + box[2]) / 2
        if abs(centre - 0.5) > 0.05:  # remember which way they were drifting; keep it when centred
            m['side'] = 'RIGHT' if centre > 0.5 else 'LEFT'
        if self.aim(box, FOLLOW_TURN_TOLERANCE):
            m['status'] = 'Turning toward you'
            return
        if self.sonar is not None:
            far, near = self.sonar > FOLLOW_FAR_CM, self.sonar < FOLLOW_NEAR_CM
        else:
            far, near = box[3] - box[1] < FOLLOW_BOX_FAR, False
        if far:
            step = GOTO_STEP_CM if self.sonar is None else max(3, min(GOTO_STEP_CM, self.sonar - FOLLOW_NEAR_CM))
            m['status'] = 'Following you' + (f' · {self.sonar} cm' if self.sonar is not None else '')
            self.forward_step(step)
        else:
            m['status'] = 'Waiting at a safe distance' if near or self.sonar is not None else 'Close enough · waiting'
            self.pause(0.3)

    # ---------- loop entry ----------
    def mission_step(self):
        kind = self.mission['kind']
        if kind in ('route', 'home'):
            self.route_step()
        elif kind == 'goto':
            self.goto_step()
        elif kind == 'follow':
            self.follow_step()
