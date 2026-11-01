import math
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import server  # noqa: F401  (sets up the import path for the laptop helpers)
import detector
import missions
import vr
from test_vr import AutopilotTests, GeoLink, wait_for


class FakeImage:
    """Stands in for a camera frame; the fake detector reads the car pose instead."""


def fake_detector(link, objects):
    """Objects: {label: (east, north)}. Visible within the camera's field of view, box grows when near."""
    def detect(_image):
        found = []
        for label, (e, n) in objects.items():
            dx, dy = e - link.east, n - link.north
            dist = math.hypot(dx, dy)
            bearing = (math.degrees(math.atan2(dx, dy)) - link.heading + 180) % 360 - 180
            if abs(bearing) < missions.HFOV_DEG / 2 and dist < 400:
                centre = 0.5 + bearing / missions.HFOV_DEG
                height = min(0.95, 25 / max(dist, 1))
                found.append(dict(label=label, conf=0.9, box=(centre - 0.05, 0.6 - height / 2, centre + 0.05, 0.6 + height / 2)))
        return found
    return detect


class SonarLink(GeoLink):
    """GeoLink whose sonar sees any object in a dict (bottle, person...)."""
    def __init__(self, app, objects=None):
        super().__init__(app)
        self.objects = objects or {}

    def sonar(self):
        best = 150
        for e, n in self.objects.values():
            dx, dy = e - self.east, n - self.north
            dist = math.hypot(dx, dy)
            bearing = (math.degrees(math.atan2(dx, dy)) - self.heading + 180) % 360 - 180
            if abs(bearing) < 15 and dist < 80:
                best = min(best, round(dist))
        return best


class ParseTests(unittest.TestCase):
    def test_new_commands(self):
        p = vr.parse_command
        self.assertEqual(p('record route'), [('record_start',)])
        self.assertEqual(p('start recording'), [('record_start',)])
        self.assertEqual(p('stop recording'), [('record_stop',)])
        self.assertEqual(p('save route'), [('record_stop',)])
        self.assertEqual(p('repeat route'), [('repeat',)])
        self.assertEqual(p('replay'), [('repeat',)])
        self.assertEqual(p('go home'), [('home',)])
        self.assertEqual(p('come back'), [('home',)])
        self.assertEqual(p('go to the bottle'), [('goto', 'bottle')])
        self.assertEqual(p('find my bag'), [('goto', 'backpack')])
        self.assertEqual(p('come to me'), [('goto', 'person')])
        self.assertEqual(p('follow me'), [('follow',)])
        self.assertEqual(p('stop following'), [('mission_off',)])
        self.assertEqual(p('what do you see'), [('see',)])
        with self.assertRaises(ValueError):
            p('go to the moon')

    def test_existing_commands_unchanged(self):
        p = vr.parse_command
        self.assertEqual(p('stop'), [('stop',)])
        self.assertEqual(p('forward'), [('move', 'FORWARD', None)])
        self.assertEqual(p('go forward 20 cm'), [('move', 'FORWARD', 20)])
        self.assertEqual(p('turn right 30 degrees and move forward'), [('turn', 'RIGHT', 30), ('move', 'FORWARD', None)])
        self.assertEqual(p('autopilot 200 cm'), [('auto', 200)])
        self.assertEqual(p('resume'), [('resume',)])

    def test_simplify_keeps_corners(self):
        path = [[0, 0], [0, 10], [0, 20], [0, 40], [10, 40], [30, 40]]
        self.assertEqual(missions.simplify(path), [[0, 0], [0, 40], [30, 40]])

    def test_scene_description(self):
        dets = [dict(label='bottle', conf=0.9, box=(0.45, 0.4, 0.55, 0.8)), dict(label='chair', conf=0.8, box=(0.0, 0.2, 0.2, 0.9))]
        self.assertEqual(missions.describe_scene(dets, 42), 'I see a bottle ahead, about 42 cm away and a chair on the left.')
        self.assertEqual(missions.describe_scene([], None), 'I do not see any objects I know.')


class MissionTests(AutopilotTests):
    """Reuses the simulated VR session from test_vr (GeoLink car, heartbeat, engine on, max speed)."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(missions, 'ROUTE_FILE', Path(folder.name) / 'route.json')
        patcher.start(); self.addCleanup(patcher.stop)

    def camera_session(self, objects):
        app, link = self.session(lambda a: SonarLink(a, objects))
        app.picture = lambda: (FakeImage(), time.time())
        patcher = patch.object(detector, 'detect', fake_detector(link, objects))
        patcher.start(); self.addCleanup(patcher.stop)
        return app, link

    # AutopilotTests' own tests are not re-run here.
    test_autopilot_goes_around_bottle_and_rejoins_line = None
    test_user_correction_is_kept_then_line_rejoined = None
    test_stuck_autopilot_asks_for_help_and_continues_after_user_steers = None
    test_hold_resume_forward_and_cancel = None

    def test_record_go_home_and_repeat(self):
        app, link = self.session(lambda a: GeoLink(a))
        app.vr.command('record route')
        self.assertTrue(app.vr.recording)
        app.vr.command('forward 40 cm')
        self.assertTrue(wait_for(lambda: link.north > 39 and not app.vr.queue, 6))
        app.vr.command('turn right 90 degrees')
        self.assertTrue(wait_for(lambda: abs(link.heading - 90) < 1, 6))
        app.vr.command('forward 30 cm')
        self.assertTrue(wait_for(lambda: link.east > 29, 6))
        time.sleep(0.3)
        app.vr.command('stop recording')
        self.assertIsNone(app.vr.recording)
        self.assertEqual(len(app.vr.taught['points']), 3)          # start, corner, end
        self.assertTrue(missions.ROUTE_FILE.exists())

        app.vr.command('go home')
        self.assertTrue(wait_for(lambda: app.vr.mission is None, 25), app.vr.mission)
        self.assertLess(math.hypot(link.east, link.north), 8)       # back at the start
        self.assertLess(abs((link.heading + 180) % 360 - 180), 6)    # facing the start direction
        self.assertIn('Home', app.vr.message)

        app.vr.command('repeat route')
        self.assertTrue(wait_for(lambda: app.vr.mission is None, 25), app.vr.mission)
        self.assertLess(math.hypot(link.east - 30, link.north - 40), 8)   # ends where the taught route ended
        self.assertLess(abs((link.heading - 90 + 180) % 360 - 180), 6)     # facing the taught final direction
        self.assertTrue(-180 <= app.vr.heading <= 180)
        self.assertIn('Route complete', app.vr.message)

    def test_repeat_without_route_explains(self):
        app, _ = self.session(lambda a: GeoLink(a))
        app.vr.taught = None
        app.vr.command('repeat route')
        self.assertIsNone(app.vr.mission)
        self.assertIn('No route saved', app.vr.message)

    def test_go_to_bottle_searches_turns_and_stops_near_it(self):
        app, link = self.camera_session({'bottle': (60.0, 70.0)})   # off to the right, not in view at the start
        app.vr.command('go to the bottle')
        self.assertTrue(wait_for(lambda: app.vr.mission is None, 25), app.vr.mission)
        self.assertIn('Reached the bottle', app.vr.message)
        gap = math.hypot(link.east - 60, link.north - 70)
        self.assertLess(gap, missions.GOTO_STOP_CM + 8)
        self.assertGreater(gap, 10)                                  # stopped before touching it
        self.assertEqual(app.vr.speech['text'], 'I reached the bottle.')

    def test_go_to_missing_object_gives_up_after_full_turn(self):
        app, link = self.camera_session({})
        app.vr.command('find the cup')
        self.assertTrue(wait_for(lambda: app.vr.mission is None, 20))
        self.assertIn('could not find a cup', app.vr.message)
        self.assertLess(math.hypot(link.east, link.north), 1)        # searched in place, never drove

    def test_follow_me_keeps_distance_and_turns_to_person(self):
        person = {'person': (0.0, 160.0)}
        app, link = self.camera_session(person)
        app.vr.command('follow me')
        self.assertTrue(wait_for(lambda: link.north > 40, 15))
        time.sleep(3)
        gap = math.hypot(link.east - 0, link.north - 160)
        self.assertLess(gap, missions.FOLLOW_FAR_CM + 12)           # caught up...
        self.assertGreater(gap, missions.FOLLOW_NEAR_CM - 12)       # ...but not too close
        for step in range(1, 8):                                      # the person walks off to the right
            person['person'] = (step * 10.0, 160.0 + step * 6)
            time.sleep(0.6)
        self.assertTrue(wait_for(lambda: link.east > 20, 20))
        app.vr.command('stop following')
        self.assertIsNone(app.vr.mission)

    def test_hold_stops_follow(self):
        app, link = self.camera_session({'person': (0.0, 300.0)})
        app.vr.command('follow me')
        self.assertTrue(wait_for(lambda: link.north > 5, 10))
        app.vr.command('hold')
        self.assertIsNone(app.vr.mission)
        time.sleep(0.4); north = link.north; time.sleep(0.6)
        self.assertAlmostEqual(link.north, north, delta=6)

    def test_missing_model_is_reported_not_crashing(self):
        app, link = self.session(lambda a: GeoLink(a))
        app.picture = lambda: (FakeImage(), time.time())
        with patch.object(detector, 'MODEL', Path('does-not-exist.onnx')), patch.object(detector, '_session', None):
            app.vr.command('go to the bottle')
            self.assertTrue(wait_for(lambda: app.vr.mission is None, 5))
        self.assertIn('not installed', app.vr.message)
        self.assertTrue(app.vr.engine)                               # session still fine
        app.vr.command('forward 10 cm')
        self.assertTrue(wait_for(lambda: link.north > 9, 5))         # normal driving still works


if __name__ == '__main__':
    unittest.main()
