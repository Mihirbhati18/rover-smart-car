"""VR View: voice driving with speed levels, danger glows, path tracking and reports.

Uses only the existing bounded manual packets (@F/@B/@L/@R, 1-350 ms). Every pulse
stops on the Arduino by itself, so "keep driving" is a chain of pulses: if the
laptop, page or link stops, the car stops within one pulse. Speed levels change
the pulse length and the pause between pulses (no firmware change).
Distances, angles, speed and the path are timing estimates, not measurements.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque

from vision_preview import inspect_image

import detector
from missions import MissionMixin

LEVEL_DEFAULT = 5
MS_PER_CM = 70             # same provisional calibration as manual routes
MS_PER_DEG = 12
TURN_PULSE_MS = 120
OVERRIDE_CM = 12           # emergency override: stop forward motion this close
DANGER_FAR_CM = 80         # glow starts
DANGER_NEAR_CM = 15        # glow at full strength
IMAGE_CLEAR = 70           # floor match below this adds to a glow
SIDE_MAX_AGE = 15          # seconds a left/right sweep reading stays on screen
HEARTBEAT_TIMEOUT = 4      # VR page gone this long -> session ends
DEFAULT_TURN_DEG = 90
FOLLOWED_MOVE_CM = 20      # "forward then ..." without a distance

# Autopilot: same idea as Pass obstacle mode, steps interleaved with the user's voice corrections.
AUTO_SAFETY_CM = 30        # centre ultrasonic at or below this counts as blocked
AUTO_IMAGE_NO_ECHO = 85    # stricter camera check when the ultrasonic has no echo
AUTO_TURN_DEG = 10
AUTO_STEP_CM = 10
AUTO_MIN_STEP_CM = 3
AUTO_MAX_HEADING = 60      # beyond this the autopilot pauses and asks for help
AUTO_MARGIN_CM = 20        # go this far past an obstacle (along the line) before rejoining
AUTO_USER_HOLD_CM = 30     # after the user steers, keep that side this far before rejoining
AUTO_MAX_GOAL_ANGLE = 45
AUTO_GOAL_TOLERANCE_CM = 5
AUTO_IMAGE_OBSTACLE_CM = 30
AUTO_CLEARANCE_CM = 22     # never plan a straight path closer than this to a remembered obstacle
AUTO_MEMORY_AHEAD_CM = 80  # only obstacles this far ahead matter for that check
AUTO_USER_HOLD_TRAVEL_CM = 45   # a correction also ends after this much driving (even sideways)
AUTO_AVOID_TRAVEL_CM = 100      # obstacle avoidance also ends after this much driving
DODGE_DEFAULT_DEG = 20

NUMBER_WORDS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
                'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12,
                'fifteen': 15, 'twenty': 20, 'thirty': 30, 'forty': 40, 'forty-five': 45,
                'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
                'hundred': 100, 'one hundred': 100}


def clamp_level(value):
    """Round to the nearest whole level (halves round up) and keep it within 1-10."""
    return max(1, min(10, math.floor(float(value) + 0.5)))


def pulse_plan(level):
    """(pulse ms, pause ms) for a speed level. Level 10 = back-to-back pulses."""
    duty = 0.15 + 0.85 * (level - 1) / 9
    pulse = min(350, 170 + 20 * (level - 1))
    return pulse, round(pulse * (1 / duty - 1))


def estimated_speed(level):
    """Approximate cm/s from the timing calibration (ignores link overhead)."""
    pulse, pause = pulse_plan(level)
    return round((pulse / MS_PER_CM) / ((pulse + pause) / 1000), 1)


def normalize(text):
    text = ' ' + text.lower().replace('°', ' deg ') + ' '
    text = re.sub(r'[^a-z0-9.,;\- ]', ' ', text)
    for word, value in sorted(NUMBER_WORDS.items(), key=lambda item: -len(item[0])):
        text = re.sub(r'\b' + word + r'\b', str(value), text)
    text = re.sub(r'\b(20|30|40|50|60|70|80|90)[ -]+([1-9])\b', lambda m: str(int(m[1]) + int(m[2])), text)
    text = re.sub(r'\bdegrees?\b', 'deg', text)
    text = re.sub(r'\bcentimet(?:er|re)s?\b|\bcms\b', 'cm', text)
    text = re.sub(r'(\d)\s*(deg|cm)\b', r'\1 \2', text)
    text = re.sub(r'\b(please|car|robot|now|okay|ok|hey)\b', ' ', text)
    return ' '.join(text.replace(' .', ' ').split()).strip(' .')


NUM = r'(-?\d+(?:\.\d+)?)'


def parse_command(text):
    """Turn one spoken/typed command into a list of actions. Unknown parts are rejected."""
    words = normalize(text)
    if not words:
        raise ValueError('No command heard.')
    clauses = re.split(r'\s*[,;]\s*(?:(?:and then|then|and)\s+)?|\s+(?:and then|then|and)\s+', words)
    actions = []
    for clause in filter(None, (c.strip(' .') for c in clauses)):
        clause = re.sub(r'^(?:the )', '', clause)
        if re.search(r'\bemergency\b', clause):
            actions.append(('estop',))
        elif re.fullmatch(r'(?:stop|end|finish|done|save)(?: the)?(?: route| path)? (?:recording|teaching)|save(?: the)? (?:route|path)', clause):
            actions.append(('record_stop',))
        elif re.fullmatch(r'(?:start |begin )?(?:recording|record|teaching|teach)(?: (?:the |a |my )?(?:route|path))?(?: mode)?|(?:start|begin) (?:the )?(?:route|path) recording', clause):
            actions.append(('record_start',))
        elif re.fullmatch(r'(?:repeat|replay|redo|run|drive)(?: the| my)?(?: (?:taught|recorded|saved|same))? (?:route|path)(?: again)?|repeat(?: it)?(?: again)?|replay|do it again', clause):
            actions.append(('repeat',))
        elif re.fullmatch(r'(?:go|come|return|drive|head)(?: back)?(?: to)? (?:home|base|the start|start|starting point)|come back|return|home|go home', clause):
            actions.append(('home',))
        elif re.fullmatch(r'(?:stop|cancel|end|quit)(?: the)? (?:following|follow(?:ing)? mode|follow me|mission|searching|search|finding|going home|route|repeat)', clause):
            actions.append(('mission_off',))
        elif re.fullmatch(r'(?:follow|come with|walk with)(?: me| the person| him| her| them)?(?: mode)?|start following(?: me)?', clause):
            actions.append(('follow',))
        elif re.fullmatch(r'what (?:do|can) you see|what(?: s| is) (?:there|ahead|in front(?: of you)?|around)|(?:describe|identify|detect)(?: (?:what you see|objects|everything|the scene))?|look around|what objects', clause):
            actions.append(('see',))
        elif m := re.fullmatch(r'(?:(?:go|drive|move|come|head|navigate)(?: up)? (?:to|towards|toward)|find|search for|look for|fetch|get|locate|approach)(?: the| a| an| my| that)? ([a-z ]+)', clause):
            label = detector.label_for(m[1])
            if not label:
                raise ValueError(f'I can find: bottle, cup, chair, person, bag, phone, laptop, book, ball, plant, tv… “{m[1]}” is not one of them.')
            actions.append(('goto', label))
        elif re.fullmatch(r'(?:(?:cancel|stop|end|exit|disable|turn off|switch off) (?:the )?auto[ -]?pilot|auto[ -]?pilot off|manual(?: mode| control)?|take over|i(?: will|ll)? take over)', clause):
            actions.append(('auto_off',))
        elif m := re.fullmatch(r'(?:(?:start|go|run|engage|use|set) )?(?:auto[ -]?pilot|auto(?:matic)? mode|auto[ -]?drive|auto)(?: mode)?(?: (?:forward|ahead|straight))?(?: (?:to|for|by))?(?: (\d+)(?: cm)?)?', clause):
            if not m[1]:
                raise ValueError('Say a distance, for example: autopilot 200 cm.')
            cm = int(m[1])
            if not 10 <= cm <= 1000:
                raise ValueError('Autopilot distance must be 10–1000 cm.')
            actions.append(('auto', cm))
        elif re.fullmatch(r'(?:resume|continue|carry on|go on|unpause)(?: (?:the )?auto[ -]?pilot)?|auto[ -]?pilot (?:on|resume|continue)', clause):
            actions.append(('resume',))
        elif m := re.fullmatch(r'(?:dodge|avoid|go around|pass|move)(?: (?:it|to the|on the|by the|towards the|to))? (left|right)(?: (?:by )?' + NUM + r'(?: deg)?)?', clause):
            degrees = DODGE_DEFAULT_DEG if m[2] is None else math.floor(float(m[2]) + 0.5)
            if not 1 <= degrees <= 360:
                raise ValueError('Turn angle must be 1–360 degrees.')
            actions.append(('turn', m[1].upper(), degrees))
        elif re.fullmatch(r'(?:stop|hold|halt|wait|brake|pause|freeze)(?: (?:moving|here|there|position|on))?', clause):
            actions.append(('stop',))
        elif m := (re.fullmatch(r'(?:(?:set|change|put|make|increase|raise|decrease|lower|reduce|go) )?(?:the )?speed(?: level)?(?: (?:to|at|of))?(?: level)? ' + NUM, clause)
                   or re.fullmatch(r'(?:(?:set|go|change|switch)(?: to)? )?level ' + NUM + r'(?: speed)?', clause)):
            actions.append(('speed', clamp_level(m[1]), m[1]))
        elif re.fullmatch(r'(?:(?:set|go|switch)(?: to)? )?(?:max|maximum|full|top) speed|speed (?:max|maximum)', clause):
            actions.append(('speed', 10, 'max'))
        elif re.fullmatch(r'(?:(?:set|go|switch)(?: to)? )?(?:min|minimum|lowest|slowest) speed|speed (?:min|minimum)', clause):
            actions.append(('speed', 1, 'min'))
        elif re.fullmatch(r'(?:increase|raise) (?:the )?speed(?: by 1| a bit)?|faster|speed up|go faster', clause):
            actions.append(('speed_delta', 1))
        elif re.fullmatch(r'(?:decrease|lower|reduce) (?:the )?speed(?: by 1| a bit)?|slower|slow down|go slower', clause):
            actions.append(('speed_delta', -1))
        elif re.fullmatch(r'(?:analy[sz]e|scan|report|check)(?: (?:the )?(?:surroundings|area|environment|everything|around))?', clause):
            if not actions or actions[-1] != ('analyze',):
                actions.append(('analyze',))
        elif m := re.fullmatch(r'(?:(?:turn|go|rotate|steer|take a) )?(left|right)(?: (?:by )?' + NUM + r'(?: deg)?)?(?: turn)?', clause):
            degrees = DEFAULT_TURN_DEG if m[2] is None else math.floor(float(m[2]) + 0.5)
            if not 1 <= degrees <= 360:
                raise ValueError('Turn angle must be 1–360 degrees.')
            actions.append(('turn', m[1].upper(), degrees))
        elif m := re.fullmatch(r'(?:(?:move|go|drive|keep (?:going|moving)|run) )?(forward|forwards|ahead|straight|back|backward|backwards|reverse)(?: (?:by|for) )?(?: ?(\d+) cm)?', clause):
            direction = 'BACKWARD' if m[1].startswith(('back', 'rev')) else 'FORWARD'
            actions.append(('move', direction, int(m[2]) if m[2] else None))
        elif re.fullmatch(r'(?:go|drive|move|start moving|keep going|go go)', clause):
            actions.append(('move', 'FORWARD', None))
        else:
            raise ValueError(f'Did not understand “{clause}”. Try: forward, stop, turn right 30 degrees, speed level 6, analyze.')
    # A continuous move followed by more actions becomes a short fixed move.
    for index, action in enumerate(actions[:-1]):
        if action[0] == 'move' and action[2] is None:
            actions[index] = ('move', action[1], FOLLOWED_MOVE_CM)
    return actions


def describe(actions, level_before):
    parts, level = [], level_before
    for action in actions:
        kind = action[0]
        if kind == 'estop': parts.append('EMERGENCY STOP')
        elif kind == 'stop': parts.append('HOLD')
        elif kind == 'speed':
            raw = action[2]
            parts.append(f'SPEED LEVEL {action[1]}' if raw in ('max', 'min') or float(raw) == action[1]
                         else f'SPEED {raw} → LEVEL {action[1]}')
            level = action[1]
        elif kind == 'speed_delta':
            level = max(1, min(10, level + action[1]))
            parts.append(f'SPEED LEVEL {level}')
        elif kind == 'analyze': parts.append('ANALYZE')
        elif kind == 'auto': parts.append(f'AUTOPILOT {action[1]} CM')
        elif kind == 'resume': parts.append('RESUME AUTOPILOT')
        elif kind == 'auto_off': parts.append('AUTOPILOT OFF')
        elif kind == 'record_start': parts.append('● RECORD ROUTE')
        elif kind == 'record_stop': parts.append('SAVE ROUTE')
        elif kind == 'repeat': parts.append('REPEAT ROUTE')
        elif kind == 'home': parts.append('GO HOME')
        elif kind == 'mission_off': parts.append('STOP MISSION')
        elif kind == 'follow': parts.append('FOLLOW ME')
        elif kind == 'see': parts.append('WHAT DO YOU SEE?')
        elif kind == 'goto': parts.append(f'GO TO {action[1].upper()}')
        elif kind == 'turn': parts.append(f'TURN {action[1]} {action[2]}°')
        elif kind == 'move':
            parts.append(action[1] + (f' {action[2]} CM' if action[2] else ''))
    return ' → '.join(parts)


def danger_level(cm):
    if cm is None:
        return 0.0
    return round(max(0.0, min(1.0, (DANGER_FAR_CM - cm) / (DANGER_FAR_CM - DANGER_NEAR_CM))), 2)


def image_danger(score):
    return round(max(0.0, (IMAGE_CLEAR - score) / IMAGE_CLEAR * 0.7), 2) if score is not None else 0.0


def build_report(scan, zones, level, motion, heading):
    """Plain-language report from a left/centre/right sweep and the camera floor match."""
    def where(cm, name, near):
        if cm is None:
            return f'{name}: no echo (nothing within range, or the reading was lost)'
        if cm <= near:
            return f'{name}: obstacle about {cm} cm away'
        return f'{name}: clear for about {cm} cm'
    left, centre, right = (scan or {}).get('left'), (scan or {}).get('centre'), (scan or {}).get('right')
    lines = [where(centre, 'Ahead', 60), where(left, 'Left', 50), where(right, 'Right', 50), 'Behind: no sensor']
    if zones:
        look = lambda s: 'floor visible' if s >= IMAGE_CLEAR else 'something different from the floor'
        lines.append(f"Camera: left {look(zones['LEFT'])} ({zones['LEFT']}%), ahead {look(zones['CENTER'])} "
                     f"({zones['CENTER']}%), right {look(zones['RIGHT'])} ({zones['RIGHT']}%)")
    else:
        lines.append('Camera: not available')
    room = {k: (v if v is not None else 150) for k, v in (('left', left), ('ahead', centre), ('right', right))}
    if zones:
        for key, zone in (('left', 'LEFT'), ('ahead', 'CENTER'), ('right', 'RIGHT')):
            if zones[zone] < IMAGE_CLEAR:
                room[key] = min(room[key], 40)
    safest = max(room, key=room.get)
    front = room['ahead']
    if front <= 25:
        advice = f'Stop or turn {safest if safest != "ahead" else "around"}. Something is very close ahead.'
    elif front <= 60:
        advice = f'Slow down. {safest.capitalize()} looks most open.' if safest != 'ahead' else 'Slow down and continue carefully.'
    else:
        advice = 'Path ahead looks clear. Continue forward.'
    lines.append(f'Status: {motion or "holding"}, speed level {level} (≈{estimated_speed(level)} cm/s est.), heading {heading:+.0f}°')
    return dict(lines=lines, advice=advice, safest=safest, time=time.strftime('%H:%M:%S'))


class VRController(MissionMixin):
    """One VR driving session, run inside the dashboard's single job slot."""

    def __init__(self, dashboard):
        self.d = dashboard
        self.lock = threading.RLock()
        self.queue = deque()
        self.active = False
        self.engine = False
        self.engine_request = False
        self.epoch = None
        self.cancel = 0
        self.beat = 0.0
        self.level = LEVEL_DEFAULT
        self.motion = None
        self.doing = 'IDLE'
        self.east = self.north = 0.0
        self.heading = 0.0
        self.odo = 0.0  # total cm driven (estimated)
        self.points = [[0.0, 0.0]]
        self.sonar = None
        self.sweep = None
        self.zones = None
        self.zones_at = 0.0
        self.report = None
        self.override_at = 0.0
        self.last_command = ''
        self.message = ''
        self.auto = None
        self.mission_init()
        self.publish()

    # ---------- state for the page ----------
    def danger(self):
        front = max(danger_level(self.sonar), image_danger(self.zones['CENTER']) if self.zones else 0)
        sides = {}
        for side, zone in (('left', 'LEFT'), ('right', 'RIGHT')):
            value = 0.0
            if self.sweep and time.time() - self.sweep['time'] < SIDE_MAX_AGE:
                value = danger_level(self.sweep.get(side))
            if self.zones:
                value = max(value, image_danger(self.zones[zone]))
            sides[side] = value
        return dict(front=front, left=sides['left'], right=sides['right'], rear=None)

    def publish(self):
        pulse, pause = pulse_plan(self.level)
        self.d.state['vr'] = dict(
            active=self.active, engine=self.engine, level=self.level, speed=estimated_speed(self.level),
            pulse_ms=pulse, pause_ms=pause, motion=self.motion, doing=self.doing,
            heading=round(self.heading), points=[p[:] for p in self.points],
            sonar=self.sonar, sweep=dict(self.sweep) if self.sweep else None, zones=self.zones,
            danger=self.danger(), report=self.report, last_command=self.last_command,
            message=self.message, override=time.time() - self.override_at < 3, auto=self.auto_view(),
            mission=self.mission_view())

    def auto_view(self):
        a = self.auto
        if not a:
            return None
        along, right, rel = self.auto_local()
        return dict(target=a['target'], along=round(along), right=round(right), heading=round(rel),
                    paused=a['paused'], help=a['help'], finished=a['finished'], status=a['status'],
                    avoid=a['avoid'], line=[a['start'], self.auto_world(a['target'], 0)],
                    obstacles=[o[:] for o in a['obstacles']])

    # ---------- autopilot geometry: along = cm toward the destination, right = cm right of the line ----------
    def auto_local(self):
        a = self.auto
        dx, dy = self.east - a['start'][0], self.north - a['start'][1]
        t = math.radians(a['h0'])
        rel = (self.heading - a['h0'] + 180) % 360 - 180
        return dx * math.sin(t) + dy * math.cos(t), dx * math.cos(t) - dy * math.sin(t), rel

    def auto_world(self, along, right):
        a, t = self.auto, math.radians(self.auto['h0'])
        return [round(a['start'][0] + along * math.sin(t) + right * math.cos(t), 1),
                round(a['start'][1] + along * math.cos(t) - right * math.sin(t), 1)]

    def auto_running(self):
        return bool(self.auto) and not self.auto['finished']

    def say(self, message):
        self.message = message
        self.publish()

    # ---------- called from HTTP requests ----------
    def command(self, text):
        actions = parse_command(text)
        if not self.active:
            raise RuntimeError('Open VR View and press START first.')
        with self.lock:
            self.last_command = describe(actions, self.level)
            self.message = ''  # cleared first, so messages set by this command stay visible
            replaced = False  # a new movement command replaces the previous one, once per command
            for action in actions:
                kind = action[0]
                if kind == 'estop':
                    self.queue.clear(); self.motion = None; self.cancel += 1
                    self.end_auto('Cancelled by emergency stop')
                    self.mission = None
                    self.d.stop()
                elif kind == 'stop':
                    self.hold()
                elif kind == 'auto_off':
                    self.cancel += 1
                    self.end_auto('Cancelled · you have manual control')
                    self.mission = None
                elif kind == 'record_start':
                    self.start_recording()
                elif kind == 'record_stop':
                    self.stop_recording()
                elif kind == 'mission_off':
                    self.cancel += 1
                    self.end_auto('Cancelled')
                    self.end_mission('Stopped. You have control.')
                elif kind in ('repeat', 'home', 'goto', 'follow'):
                    if not self.engine:
                        self.say('Engine is OFF. Press START ENGINE, then repeat the command.')
                        return
                    self.queue.clear(); self.motion = None; self.cancel += 1
                    self.end_auto('Replaced by a new mission')
                    self.auto = None
                    self.mission = None
                    {'repeat': self.start_repeat, 'home': self.start_home, 'follow': self.start_follow,
                     'goto': lambda: self.start_goto(action[1])}[kind]()
                elif kind == 'resume' or (kind == 'move' and action[2] is None and self.auto_running()):
                    # During autopilot, "forward"/"keep going" means carry on with the autopilot.
                    if not self.auto_running():
                        self.say('No autopilot to resume. Say: autopilot 200 cm.')
                        return
                    self.auto.update(paused=False, help=False, status='Running')
                elif kind == 'auto':
                    if not self.engine:
                        self.say('Engine is OFF. Press START ENGINE, then repeat the command.')
                        return
                    self.queue.clear(); self.motion = None; self.cancel += 1
                    self.mission = None
                    self.start_auto(action[1])
                elif kind == 'speed':
                    self.level = action[1]
                elif kind == 'speed_delta':
                    self.level = max(1, min(10, self.level + action[1]))
                else:
                    if kind in ('move', 'turn') and not self.engine:
                        self.say('Engine is OFF. Press START ENGINE, then repeat the command.')
                        return
                    if not replaced and (self.motion or self.queue or self.auto_running() or self.mission):
                        self.queue.clear(); self.motion = None; self.cancel += 1  # also cuts into an autopilot step
                    if kind in ('move', 'turn') and self.mission and self.mission['kind'] in ('goto', 'follow'):
                        self.end_mission('You took over.')  # steering ends object finding / following
                    replaced = True
                    self.queue.append(action)
            self.publish()

    def start_auto(self, target, final='start', quiet=False):
        """final: heading to face at the end ('start' = the starting heading, None = don't turn)."""
        self.auto = dict(target=target, start=[round(self.east, 1), round(self.north, 1)], h0=self.heading,
                         final=self.heading if final == 'start' else final,
                         paused=False, help=False, finished=False, status='Running', avoid=None, avoid_kind=None,
                         avoid_until=0.0, avoid_odo=0.0, obstacles=[], memory=[])
        if not quiet:
            self.d.log('VR', f'Autopilot: reach the point {target} cm ahead. Steer any time to correct; say "resume" after a hold.')

    def end_auto(self, status):
        if self.auto_running():
            self.auto.update(finished=True, paused=False, help=False, status=status)
            self.d.log('VR', 'Autopilot: ' + status)

    def hold(self):
        with self.lock:
            self.queue.clear()
            self.motion = None
            self.cancel += 1
            self.doing = 'HOLD'
            if self.auto_running():
                self.auto.update(paused=True, status='Paused · say "resume" to continue')
            if self.mission and self.mission['kind'] in ('goto', 'follow'):
                self.end_mission('Held. Mission stopped.')  # route/home missions pause with their autopilot leg
        link = self.d.link
        if link and link.is_open and self.d.state['mode'] == 'PRESENTATION':
            try:
                with self.d.send_lock:
                    link.send('AI_STOP')
            except Exception:
                pass
        self.publish()

    def request_engine(self):
        if not self.active:
            raise RuntimeError('VR session is not running.')
        self.engine_request = True

    def exit(self):
        self.active = False

    def reset_path(self):
        with self.lock:
            self.end_auto('Cancelled · path reset')
            self.auto = None
            self.mission = None
            self.recording = None
            self.east = self.north = 0.0
            self.heading = 0.0
            self.points = [[0.0, 0.0]]
            self.publish()

    # ---------- session loop (runs in the dashboard job thread) ----------
    def run(self):
        d = self.d
        if d.state['source'] == 'replay' or not d.link or not d.link.is_open:
            raise RuntimeError('Connect the car (Bluetooth or USB) before starting VR View.')
        self.active, self.beat = True, time.monotonic()
        self.queue.clear(); self.motion = None; self.report = None; self.message = ''
        d.vr_quiet = True
        d.log('VR', 'VR View session started.')
        try:
            self.start_engine()
            while self.active:
                if time.monotonic() - self.beat > HEARTBEAT_TIMEOUT:
                    d.log('VR', 'VR page stopped responding; session ended.')
                    break
                if not d.link or not d.link.is_open:
                    d.log('VR', 'Car connection lost; session ended.')
                    break
                if self.engine_request:
                    self.engine_request = False
                    self.start_engine()
                if self.engine and not self.engine_ok():
                    self.engine = False
                    with self.lock:
                        self.queue.clear(); self.motion = None
                    self.doing = 'ENGINE OFF'
                    if self.auto_running():
                        self.auto.update(paused=True, status='Paused · engine off. Start engine, then say "resume"')
                    self.say('Engine switched OFF (STOP, remote, or controls disabled). Press START ENGINE.')
                try:
                    action = None
                    with self.lock:
                        if self.queue:
                            action = self.queue.popleft()
                    if action:
                        self.execute(action)
                    elif self.motion and self.engine:
                        self.drive_cycle()
                    elif self.engine and self.auto_running() and not self.auto['paused']:
                        self.auto_step()
                    elif self.engine and self.mission and not self.auto_running():
                        self.mission_step()
                    else:
                        if self.doing not in ('HOLD', 'ENGINE OFF'):
                            self.doing = 'IDLE'
                        self.sense()
                        self.publish()
                        self.pause(0.35)
                except Exception as exc:
                    with self.lock:
                        self.queue.clear(); self.motion = None
                    self.doing = 'HOLD'
                    if self.auto_running():
                        self.auto.update(paused=True, status='Paused after a problem · say "resume" to retry')
                    if self.mission and self.mission['kind'] in ('goto', 'follow'):
                        self.mission = None
                    self.say(f'Stopped: {exc}')
                    d.log('VR', f'Stopped: {exc}')
        finally:
            self.motion = None
            self.active = False
            self.engine = False
            d.vr_quiet = False
            try:
                d.disarm()
            except Exception:
                pass
            self.doing = 'OFF'
            self.publish()
            d.log('VR', 'VR View session closed. Car stopped.')

    def engine_ok(self):
        d = self.d
        return d.state['armed'] and d.epoch == self.epoch and not d.state['emergency']

    def start_engine(self):
        d = self.d
        try:
            if d.state['emergency']:
                d.clear()
            if d.state['mode'] != 'PRESENTATION':
                d.mode()
            d.arm(manual=True)
            self.epoch = d.epoch
            self.engine = True
            self.doing = 'IDLE'
            self.say('')
            d.log('VR', 'Engine ON. Voice driving ready.')
        except Exception as exc:
            self.engine = False
            self.say(f'Could not start engine: {exc}')

    def pause(self, seconds):
        """Sleep, but wake early if the user holds or changes the command."""
        seq, deadline = self.cancel, time.monotonic() + seconds
        while time.monotonic() < deadline and self.active and seq == self.cancel:
            time.sleep(0.02)

    def pulse(self, direction, ms):
        d = self.d
        code = {'FORWARD': 'F', 'BACKWARD': 'B', 'LEFT': 'L', 'RIGHT': 'R'}[direction]
        with d.send_lock:
            d.valid(self.epoch)
            start = d.rx_seq
            d.link.send_manual_pulse(direction, ms)
        d.wait('AI:ACK:' + code, start, 2)
        # A HOLD sent mid-pulse stops the motors at once and answers AI:ACK:S instead of MOVE_DONE.
        deadline, ended = time.monotonic() + 2, None
        while ended is None and time.monotonic() < deadline:
            with d.lock:
                ended = next((m for s, m in d.received if s > start and m in ('AI:MOVE_DONE', 'AI:ACK:S')), None)
            if ended is None:
                time.sleep(0.01)
        if ended is None:
            raise TimeoutError('Arduino did not confirm the pulse ended.')
        if ended == 'AI:ACK:S':
            ms = ms / 2  # stopped early; record roughly half the movement
        with self.lock:
            if direction in ('LEFT', 'RIGHT'):
                self.heading += (ms / MS_PER_DEG) * (1 if direction == 'RIGHT' else -1)
                self.heading = (self.heading + 180) % 360 - 180  # keep within -180..180
            else:
                cm = ms / MS_PER_CM * (1 if direction == 'FORWARD' else -1)
                rad = math.radians(self.heading)
                self.east += cm * math.sin(rad)
                self.north += cm * math.cos(rad)
                self.odo += abs(cm)
                self.points.append([round(self.east, 1), round(self.north, 1)])

    def drive_cycle(self):
        direction = self.motion
        pulse, pause = pulse_plan(self.level)
        self.doing = 'MOVING ' + direction
        self.pulse(direction, pulse)
        started = time.monotonic()
        self.sense()
        self.publish()
        self.pause(max(0.0, pause / 1000 - (time.monotonic() - started)))

    def execute(self, action):
        kind = action[0]
        seq = self.cancel
        if kind == 'analyze':
            self.analyze()
        elif kind == 'see':
            self.see()
        elif kind == 'turn':
            _, side, degrees = action
            self.doing = f'TURNING {side} {degrees}°'
            self.publish()
            remaining = degrees * MS_PER_DEG
            while remaining > 0 and seq == self.cancel and self.engine:
                chunk = min(TURN_PULSE_MS, remaining)
                self.pulse(side, chunk)
                remaining -= chunk
                self.publish()
            self.doing = 'HOLD'
            if self.auto_running():
                self.user_correction(side)
        elif kind == 'move':
            _, direction, cm = action
            if cm is None:
                self.motion = direction
                return
            self.doing = f'MOVING {direction} {cm} CM'
            remaining = cm * MS_PER_CM
            while remaining > 0 and seq == self.cancel and self.engine:
                pulse, pause = pulse_plan(self.level)
                chunk = min(pulse, remaining)
                self.pulse(direction, chunk)
                remaining -= chunk
                self.sense()
                self.publish()
                if remaining > 0:
                    self.pause(pause / 1000)
            self.doing = 'HOLD'
        self.publish()

    # ---------- autopilot ----------
    def user_correction(self, side):
        """The user steered during autopilot: keep that side for a while, then rejoin the line."""
        a = self.auto
        along, _, _ = self.auto_local()
        a['avoid'], a['avoid_kind'] = side, 'user'
        a['avoid_until'] = along + AUTO_USER_HOLD_CM
        a['avoid_odo'] = self.odo + AUTO_USER_HOLD_TRAVEL_CM
        a['memory'] = []  # the user can see better than the sensor; trust their steering
        if a['help']:
            a.update(help=False, paused=False)  # the user gave the help it asked for
        a['status'] = f'Your correction: keeping {side.lower()} for {AUTO_USER_HOLD_CM} cm, then rejoining the line'
        self.d.log('VR', 'Autopilot: ' + a['status'])

    def auto_look(self):
        """Stop-and-check: fresh camera frame and one ultrasonic reading."""
        if self.d.camera:
            time.sleep(0.25)
            try:
                self.d.camera.wait_for_frame_after(time.time(), timeout=2)
            except Exception:
                pass
        self.zones_at = 0
        self.sense()
        return self.sonar, self.zones

    def auto_blocked(self, distance, zones):
        if distance is not None and distance - AUTO_SAFETY_CM < AUTO_MIN_STEP_CM:
            return f'ultrasonic {distance} cm'
        if zones:
            limit = IMAGE_CLEAR if distance is not None else AUTO_IMAGE_NO_ECHO
            if zones['CENTER'] < limit:
                return f"camera centre {zones['CENTER']}%"
        return None

    def auto_turn(self, side):
        self.pulse(side, AUTO_TURN_DEG * MS_PER_DEG)

    def auto_avoid_turn(self, side, reason, rel):
        a = self.auto
        if abs(rel + (AUTO_TURN_DEG if side == 'RIGHT' else -AUTO_TURN_DEG)) > AUTO_MAX_HEADING:
            a.update(paused=True, help=True, status=f'Needs help: {reason}. Steer me left or right — I continue after that')
            self.doing = 'AUTOPILOT · NEEDS HELP'
            self.d.log('VR', 'Autopilot: ' + a['status'])
            return
        a['avoid'], a['avoid_kind'] = side, 'obstacle'
        a['status'] = f'{reason.capitalize()} · turning {side.lower()}'
        self.doing = f'AUTOPILOT · AVOIDING {side}'
        self.publish()
        self.auto_turn(side)

    def auto_too_close(self, along, right, rel):
        """A remembered obstacle the straight path ahead would pass too closely: (gap cm, side to turn)."""
        ux, uy = math.cos(math.radians(rel)), math.sin(math.radians(rel))
        worst = None
        for oa, orr in self.auto['memory']:
            va, vr_ = oa - along, orr - right
            ahead = va * ux + vr_ * uy
            offset = vr_ * ux - va * uy  # > 0: obstacle is right of the path
            if 0 < ahead < AUTO_MEMORY_AHEAD_CM and abs(offset) < AUTO_CLEARANCE_CM:
                if worst is None or abs(offset) < abs(worst[0]):
                    worst = (offset, 'LEFT' if offset > 0 else 'RIGHT')
        return (round(abs(worst[0])), worst[1]) if worst else None

    def auto_step(self):
        """One autopilot decision and one short action, so voice commands can cut in between."""
        a = self.auto
        seq = self.cancel
        along, right, rel = self.auto_local()
        if along >= a['target'] - AUTO_GOAL_TOLERANCE_CM:
            self.doing = 'AUTOPILOT · ALIGNING'
            final = a.get('final', a['h0'])
            off = 0 if final is None else (self.heading - final + 180) % 360 - 180
            while abs(off) >= AUTO_TURN_DEG and seq == self.cancel and self.engine:
                self.auto_turn('LEFT' if off > 0 else 'RIGHT')
                off = (self.heading - final + 180) % 360 - 180
                self.publish()
            if seq == self.cancel:
                self.end_auto(f'Destination reached · {abs(round(right))} cm {"right" if right >= 0 else "left"} of the line')
                self.doing = 'HOLD'
            return
        self.doing = 'AUTOPILOT · CHECKING'
        self.publish()
        distance, zones = self.auto_look()
        if seq != self.cancel or a['paused'] or a['finished']:
            return
        reason = self.auto_blocked(distance, zones)
        if reason:
            ahead = distance if distance is not None and distance - AUTO_SAFETY_CM < AUTO_MIN_STEP_CM else AUTO_IMAGE_OBSTACLE_CM
            r = math.radians(rel)
            obstacle = (along + ahead * math.cos(r), right + ahead * math.sin(r))
            a['obstacles'].append(self.auto_world(*obstacle))
            a['memory'].append(obstacle)
            a['avoid_until'] = max(a['avoid_until'], obstacle[0] + AUTO_MARGIN_CM)
            a['avoid_odo'] = max(a['avoid_odo'], self.odo + AUTO_AVOID_TRAVEL_CM)
            side = a['avoid'] or (('RIGHT' if zones['RIGHT'] >= zones['LEFT'] else 'LEFT') if zones else 'RIGHT')
            self.auto_avoid_turn(side, f'blocked ({reason})', rel)
            return
        goal = math.degrees(math.atan2(-right, max(a['target'] - along, 1)))
        goal = max(-AUTO_MAX_GOAL_ANGLE, min(AUTO_MAX_GOAL_ANGLE, goal))
        if a['avoid']:
            done = along >= a['avoid_until'] or self.odo >= a['avoid_odo']
            if a['avoid_kind'] == 'obstacle' and self.auto_too_close(along, right, goal) is None:
                done = True  # the way to the destination now clears every remembered obstacle
            if done:
                a['avoid'] = None
                a['status'] = 'Past the obstacle · rejoining the line'
        close = self.auto_too_close(along, right, rel)
        if close:
            # The sensor lost sight of it, but the remembered obstacle is still too near this path.
            gap, away = close
            self.auto_avoid_turn(a['avoid'] or away, f'remembered obstacle would pass {gap} cm away', rel)
            return
        if not a['avoid'] and abs(goal - rel) >= AUTO_TURN_DEG * 0.75:
            side = 'RIGHT' if goal > rel else 'LEFT'
            self.doing = f'AUTOPILOT · STEERING {side}'
            self.publish()
            self.auto_turn(side)
            return
        r = math.radians(rel)
        step = min(AUTO_STEP_CM, max(AUTO_MIN_STEP_CM, round((a['target'] - along) / max(math.cos(r), 0.3))))
        if distance is not None:
            step = min(step, distance - AUTO_SAFETY_CM)
        if not a['avoid'] and not a['status'].startswith('Past'):
            a['status'] = 'Running'
        self.doing = 'AUTOPILOT · FORWARD'
        self.publish()
        remaining = step * MS_PER_CM
        while remaining > 0 and seq == self.cancel and self.engine and not a['paused']:
            pulse, pause = pulse_plan(self.level)
            chunk = min(pulse, remaining)
            self.pulse('FORWARD', chunk)
            remaining -= chunk
            self.publish()
            if remaining > 0:
                self.pause(pause / 1000)

    def read_sonar(self):
        d = self.d
        try:
            with d.send_lock:
                start = d.rx_seq
                d.link.send('AI_DISTANCE')
            line = d.wait('AI:DIST', start, 1)
        except Exception:
            return None
        match = re.fullmatch(r'AI:DIST_CM=(\d+)', line)
        return int(match[1]) if match else None

    def sense(self):
        """Live front distance plus camera zones; triggers the emergency override."""
        if self.d.state['mode'] == 'PRESENTATION':
            self.sonar = self.read_sonar()
        if time.monotonic() - self.zones_at > 0.6:
            self.zones_at = time.monotonic()
            try:
                self.zones = inspect_image(self.d.picture()[0]).clear_percent
            except Exception:
                self.zones = None
        if self.motion == 'FORWARD' and self.sonar is not None and self.sonar <= OVERRIDE_CM:
            with self.lock:
                self.motion = None
                self.queue.clear()
                self.cancel += 1
            self.doing = 'HOLD'
            self.override_at = time.time()
            self.say(f'EMERGENCY OVERRIDE: object {self.sonar} cm ahead. Stopped.')
            self.d.log('VR', f'Emergency override: object {self.sonar} cm ahead, forward motion stopped.')

    def analyze(self):
        self.doing = 'ANALYZING'
        with self.lock:
            self.motion = None
        self.publish()
        scan = self.d.scan()
        if scan:
            self.sweep = dict(scan, time=time.time())
            self.sonar = scan.get('centre')
        self.zones_at = 0
        self.sense()
        self.report = build_report(scan, self.zones, self.level, None, self.heading)
        self.d.log('VR', 'Report: ' + ' | '.join(self.report['lines'][:3]) + ' → ' + self.report['advice'])
        self.doing = 'HOLD'
        self.publish()
