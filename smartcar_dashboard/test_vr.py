import threading
import time
import unittest
from unittest.mock import patch

import server
import vr


class ParseTests(unittest.TestCase):
    def test_levels_round_and_clamp(self):
        self.assertEqual([vr.clamp_level(v) for v in (7.6, 4.4, 5.5, 12, 50, 0, -3, '3')], [8, 4, 6, 10, 10, 1, 1, 3])

    def test_speed_commands(self):
        p = vr.parse_command
        self.assertEqual(p('Set speed to level 3'), [('speed', 3, '3')])
        self.assertEqual(p('increase speed to level 6')[0][:2], ('speed', 6))
        self.assertEqual(p('decrease speed to level two')[0][:2], ('speed', 2))
        self.assertEqual(p('speed 7.6')[0][:2], ('speed', 8))
        self.assertEqual(p('maximum speed'), [('speed', 10, 'max')])
        self.assertEqual(p('slower'), [('speed_delta', -1)])
        self.assertEqual(vr.describe(p('speed 7.6'), 5), 'SPEED 7.6 → LEVEL 8')

    def test_driving_commands(self):
        p = vr.parse_command
        self.assertEqual(p('Forward'), [('move', 'FORWARD', None)])
        self.assertEqual(p('turn right 30 degrees and move forward'), [('turn', 'RIGHT', 30), ('move', 'FORWARD', None)])
        self.assertEqual(p('Turn left 45 degrees'), [('turn', 'LEFT', 45)])
        self.assertEqual(p('turn left forty five degrees'), [('turn', 'LEFT', 45)])
        self.assertEqual(p('right'), [('turn', 'RIGHT', 90)])
        self.assertEqual(p('reverse 20 cm'), [('move', 'BACKWARD', 20)])
        self.assertEqual(p('forward then turn left 30 degrees'), [('move', 'FORWARD', vr.FOLLOWED_MOVE_CM), ('turn', 'LEFT', 30)])
        self.assertEqual(p('Hold'), [('stop',)])
        self.assertEqual(p('analyze and report'), [('analyze',)])
        self.assertEqual(p('emergency stop'), [('estop',)])

    def test_unknown_is_rejected(self):
        for bad in ('dance', 'forward and fly', ''):
            with self.assertRaises(ValueError):
                vr.parse_command(bad)

    def test_pulse_plan_slower_levels_are_slower(self):
        speeds = [vr.estimated_speed(level) for level in range(1, 11)]
        self.assertEqual(speeds, sorted(speeds))
        self.assertEqual(vr.pulse_plan(10), (350, 0))
        for level in range(1, 11):
            pulse, pause = vr.pulse_plan(level)
            self.assertTrue(1 <= pulse <= 350 and pause >= 0)

    def test_danger_scale(self):
        self.assertEqual(vr.danger_level(None), 0)
        self.assertEqual(vr.danger_level(200), 0)
        self.assertEqual(vr.danger_level(10), 1)
        self.assertTrue(0 < vr.danger_level(40) < 1)

    def test_report(self):
        report = vr.build_report({'left': 120, 'centre': 40, 'right': 25}, {'LEFT': 95, 'CENTER': 30, 'RIGHT': 90}, 5, None, 0)
        text = ' '.join(report['lines'])
        self.assertIn('Ahead: obstacle about 40 cm', text)
        self.assertIn('Right: obstacle about 25 cm', text)
        self.assertEqual(report['safest'], 'left')
        self.assertTrue(report['advice'].startswith('Slow down'))
        self.assertIn('Behind: no sensor', text)


class FakeLink:
    def __init__(self, app):
        self.app, self.is_open, self.pulses, self.distance = app, True, [], 150

    def emit(self, message):
        with self.app.lock:
            self.app.rx_seq += 1
            self.app.received.append((self.app.rx_seq, message))

    def send(self, command):
        if command == 'STATUS': self.emit('STATUS:MODE=PRESENTATION,MOTION=0,EMERGENCY=0')
        elif command == 'PRESENTATION': self.emit('PRESENTATION:READY')
        elif command == 'AI_STOP': self.emit('AI:ACK:S')
        elif command == 'AI_DISTANCE': self.emit(f'AI:DIST_CM={self.distance}')
        elif command == 'AI_SCAN': self.emit('AI:SCAN:L=120,C=60,R=30')
        elif command == 'EMERGENCY_STOP': self.emit('EMERGENCY:STOPPED')

    def send_manual_pulse(self, direction, ms):
        self.pulses.append((direction, ms))
        self.emit('AI:ACK:' + {'FORWARD': 'F', 'BACKWARD': 'B', 'LEFT': 'L', 'RIGHT': 'R'}[direction])
        self.emit('AI:MOVE_DONE')

    def disconnect(self, send_stop=True):
        self.is_open = False


def wait_for(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class SessionTests(unittest.TestCase):
    @patch.object(server.Dashboard, 'watchdog', return_value=None)
    def test_full_session(self, _):
        app = server.Dashboard()
        link = FakeLink(app)
        app.link = link
        app.state.update(source='live', serial_connected=True, manual_firmware=True, mode='IDLE')
        app.picture = lambda: (_ for _ in ()).throw(RuntimeError('no camera'))
        alive = threading.Event()

        def beat():
            while not alive.is_set():
                app.last_heartbeat = app.vr.beat = time.monotonic()
                time.sleep(0.1)
        threading.Thread(target=beat, daemon=True).start()
        try:
            app.job('vr', {})
            self.assertTrue(wait_for(lambda: app.vr.engine), 'engine should start')
            self.assertTrue(app.state['armed'] and app.state['manual_only'])

            app.vr.command('forward')
            self.assertTrue(wait_for(lambda: sum(1 for p in link.pulses if p[0] == 'FORWARD') >= 2))
            self.assertEqual(link.pulses[0], ('FORWARD', vr.pulse_plan(5)[0]))

            app.vr.command('stop')
            self.assertTrue(wait_for(lambda: app.vr.motion is None))
            time.sleep(0.3); count = len(link.pulses); time.sleep(0.5)
            self.assertEqual(len(link.pulses), count, 'no pulses after hold')

            app.vr.command('speed 7.6')
            self.assertEqual(app.vr.level, 8)
            start = len(link.pulses)
            app.vr.command('turn right 30 degrees and move forward')
            self.assertTrue(wait_for(lambda: any(p[0] == 'FORWARD' for p in link.pulses[start:])))
            self.assertEqual(link.pulses[start:start + 3], [('RIGHT', 120)] * 3)
            self.assertIn(('FORWARD', vr.pulse_plan(8)[0]), link.pulses[start + 3:])
            self.assertAlmostEqual(app.vr.heading, 30, delta=0.5)

            link.distance = 10  # something appears right in front
            self.assertTrue(wait_for(lambda: app.vr.motion is None))
            self.assertIn('OVERRIDE', app.state['vr']['message'])
            self.assertFalse(app.state['emergency'])  # override holds, it does not latch E-stop
            link.distance = 150

            app.vr.command('analyze')
            self.assertTrue(wait_for(lambda: app.state['vr']['report'] is not None))
            self.assertIn('Right: obstacle about 30 cm', ' '.join(app.state['vr']['report']['lines']))

            app.vr.exit()
            self.assertTrue(wait_for(lambda: not app.state['busy']))
            self.assertFalse(app.state['armed'])
            self.assertFalse(app.state['vr']['active'])
        finally:
            alive.set()
            app.vr.exit()

    @patch.object(server.Dashboard, 'watchdog', return_value=None)
    def test_session_ends_when_page_stops_beating(self, _):
        app = server.Dashboard()
        app.link = FakeLink(app)
        app.state.update(source='live', serial_connected=True, manual_firmware=True, mode='IDLE')
        app.picture = lambda: (_ for _ in ()).throw(RuntimeError('no camera'))
        with patch.object(vr, 'HEARTBEAT_TIMEOUT', 0.5):
            app.job('vr', {})
            self.assertTrue(wait_for(lambda: app.vr.active))
            self.assertTrue(wait_for(lambda: not app.state['busy'], 3))
        self.assertFalse(app.state['armed'])

    def test_commands_need_a_session(self):
        with patch.object(server.Dashboard, 'watchdog', return_value=None):
            app = server.Dashboard()
        with self.assertRaises(RuntimeError):
            app.vr.command('forward')


class AutoParseTests(unittest.TestCase):
    def test_autopilot_commands(self):
        p = vr.parse_command
        self.assertEqual(p('autopilot 200 cm'), [('auto', 200)])
        self.assertEqual(p('auto pilot forward 150 centimetres'), [('auto', 150)])
        self.assertEqual(p('start autopilot to 300'), [('auto', 300)])
        self.assertEqual(p('resume'), [('resume',)])
        self.assertEqual(p('continue autopilot'), [('resume',)])
        self.assertEqual(p('cancel autopilot'), [('auto_off',)])
        self.assertEqual(p('manual control'), [('auto_off',)])
        self.assertEqual(p('dodge left'), [('turn', 'LEFT', vr.DODGE_DEFAULT_DEG)])
        self.assertEqual(p('avoid to the right 30 degrees'), [('turn', 'RIGHT', 30)])
        with self.assertRaises(ValueError): p('autopilot')
        with self.assertRaises(ValueError): p('autopilot 5000 cm')


class GeoLink(FakeLink):
    """Fake car that moves by the timing model; the sonar only sees a bottle it points at."""
    def __init__(self, app, bottle=None, fixed_distance=None):
        super().__init__(app)
        self.east = self.north = self.heading = 0.0
        self.bottle, self.fixed = bottle, fixed_distance
        self.track = [(0.0, 0.0)]

    def sonar(self):
        if self.fixed is not None:
            return self.fixed
        if not self.bottle:
            return 150
        import math
        dx, dy = self.bottle[0] - self.east, self.bottle[1] - self.north
        dist = math.hypot(dx, dy)
        bearing = math.degrees(math.atan2(dx, dy)) - self.heading
        bearing = (bearing + 180) % 360 - 180
        return round(dist) if abs(bearing) < 15 and dist < 80 else 150

    def send(self, command):
        if command == 'AI_DISTANCE':
            self.emit(f'AI:DIST_CM={self.sonar()}')
        else:
            super().send(command)

    def send_manual_pulse(self, direction, ms):
        import math
        time.sleep(ms / 1000 * 0.25)  # pulses take (scaled) real time, so commands can cut in
        if direction in ('LEFT', 'RIGHT'):
            self.heading += ms / vr.MS_PER_DEG * (1 if direction == 'RIGHT' else -1)
        else:
            cm = ms / vr.MS_PER_CM * (1 if direction == 'FORWARD' else -1)
            self.east += cm * math.sin(math.radians(self.heading))
            self.north += cm * math.cos(math.radians(self.heading))
            self.track.append((self.east, self.north))
        super().send_manual_pulse(direction, ms)


class AutopilotTests(unittest.TestCase):
    def session(self, link_factory):
        patcher = patch.object(server.Dashboard, 'watchdog', return_value=None)
        patcher.start(); self.addCleanup(patcher.stop)
        app = server.Dashboard()
        link = link_factory(app)
        app.link = link
        app.state.update(source='live', serial_connected=True, manual_firmware=True, mode='IDLE')
        app.picture = lambda: (_ for _ in ()).throw(RuntimeError('no camera'))
        alive = threading.Event()

        def beat():
            while not alive.is_set():
                app.last_heartbeat = app.vr.beat = time.monotonic()
                time.sleep(0.1)
        threading.Thread(target=beat, daemon=True).start()
        self.addCleanup(alive.set)
        self.addCleanup(app.vr.exit)
        app.job('vr', {})
        self.assertTrue(wait_for(lambda: app.vr.engine))
        app.vr.command('maximum speed')
        return app, link

    def test_autopilot_goes_around_bottle_and_rejoins_line(self):
        import math
        app, link = self.session(lambda a: GeoLink(a, bottle=(0.0, 40.0)))
        app.vr.command('autopilot 150 cm')
        self.assertTrue(wait_for(lambda: app.vr.auto and app.vr.auto['finished'], 15), app.vr.auto)
        self.assertIn('Destination reached', app.vr.auto['status'])
        closest = min(math.hypot(e - 0, n - 40) for e, n in link.track)
        self.assertGreater(closest, 12)                 # never drove into the bottle
        self.assertGreater(max(e for e, _ in link.track), 8)  # really went around it
        self.assertLess(abs(link.east), 10)             # back on the line
        self.assertGreater(link.north, 140)
        self.assertLess(abs(link.heading), vr.AUTO_TURN_DEG)
        self.assertTrue(app.state['vr']['auto']['obstacles'])

    def test_user_correction_is_kept_then_line_rejoined(self):
        app, link = self.session(lambda a: GeoLink(a))
        app.vr.command('autopilot 120 cm')
        self.assertTrue(wait_for(lambda: link.north > 15))
        app.vr.command('dodge left')
        self.assertTrue(wait_for(lambda: app.vr.auto['avoid'] == 'LEFT'))
        self.assertTrue(wait_for(lambda: app.vr.auto['finished'], 15), app.vr.auto)
        self.assertLess(min(e for e, _ in link.track), -5)   # it kept the user's side for a while
        self.assertLess(abs(link.east), 10)                  # then rejoined the line
        self.assertIn('Destination reached', app.vr.auto['status'])

    def test_stuck_autopilot_asks_for_help_and_continues_after_user_steers(self):
        app, link = self.session(lambda a: GeoLink(a, fixed_distance=20))
        app.vr.command('autopilot 100 cm')
        self.assertTrue(wait_for(lambda: app.vr.auto['help'], 10))
        self.assertTrue(app.vr.auto['paused'])
        link.fixed = None  # the user can see a way out
        app.vr.command('right 30 degrees')
        self.assertTrue(wait_for(lambda: not app.vr.auto['help'] and not app.vr.auto['paused']))
        self.assertTrue(wait_for(lambda: app.vr.auto['finished'], 15))

    def test_hold_resume_forward_and_cancel(self):
        app, link = self.session(lambda a: GeoLink(a))
        app.vr.command('autopilot 300 cm')
        self.assertTrue(wait_for(lambda: link.north > 5))
        app.vr.command('hold')
        self.assertTrue(app.vr.auto['paused'])
        time.sleep(0.4); north = link.north; time.sleep(0.5)
        self.assertAlmostEqual(link.north, north, delta=6)   # at most the pulse in flight
        app.vr.command('resume')
        self.assertTrue(wait_for(lambda: link.north > north + 10))
        app.vr.command('hold'); app.vr.command('forward')     # "forward" during autopilot = resume
        self.assertFalse(app.vr.auto['paused'])
        app.vr.command('cancel autopilot')
        self.assertTrue(app.vr.auto['finished'])
        self.assertIn('Cancelled', app.vr.auto['status'])
        time.sleep(0.4); north = link.north; time.sleep(0.5)
        self.assertAlmostEqual(link.north, north, delta=6)


if __name__ == '__main__':
    unittest.main()
