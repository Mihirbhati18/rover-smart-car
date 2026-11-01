import unittest
from unittest.mock import patch
import server


class DecisionTests(unittest.TestCase):
    def test_image_selects_left_when_clearance_passes(self):
        self.assertEqual(server.decide({'LEFT':93,'CENTER':63,'RIGHT':61}, {'left':68,'centre':29,'right':35})[0], 'LEFT')

    def test_ultrasonic_vetoes_image(self):
        scores = {'LEFT':93,'CENTER':63,'RIGHT':61}
        for scan in (None, {'left':None,'centre':29,'right':35}, {'left':20,'centre':29,'right':70}, {'left':68,'centre':20,'right':35}):
            self.assertEqual(server.decide(scores, scan)[0], 'STOP')

    def test_no_turn_for_uncertain_image(self):
        self.assertEqual(server.decide({'LEFT':75,'CENTER':70,'RIGHT':73}, {'left':100,'centre':100,'right':100})[0], 'STOP')

    def test_forward_needs_measured_room(self):
        scores = {'LEFT':94,'CENTER':96,'RIGHT':61}
        self.assertEqual(server.decide(scores, {'left':90,'centre':55,'right':90})[0], 'STOP')
        self.assertEqual(server.decide(scores, {'left':90,'centre':80,'right':90})[0], 'FORWARD')

    def test_voice_rejects_negation_and_ambiguity(self):
        for text in ('not left','do not move forward','left then right','forward backward','something random'):
            self.assertIsNone(server.voice_direction(text))
        self.assertEqual(server.voice_direction('please turn left'), 'LEFT')
        self.assertEqual(server.voice_direction('stop go forward'), 'STOP')

    def test_camera_url_scope(self):
        self.assertEqual(server.camera_address('192.168.1.4:8080'), 'http://192.168.1.4:8080/videofeed')
        for url in ('file:///etc/passwd','http://127.0.0.1:80','https://example.com','http://8.8.8.8'):
            with self.assertRaises(ValueError): server.camera_address(url)

    @patch.object(server.Dashboard, 'watchdog', return_value=None)
    def test_stop_cancels_pending_step_and_arming_is_required(self, _):
        app = server.Dashboard()
        with self.assertRaises(RuntimeError): app.valid(app.epoch)
        app.arm()
        epoch = app.epoch
        app.valid(epoch)
        app.stop()
        with self.assertRaises(RuntimeError): app.move('LEFT', {'left':90,'centre':90,'right':90}, epoch)
        self.assertFalse(app.state['armed'])
        self.assertTrue(app.state['emergency'])

    @patch.object(server.Dashboard, 'watchdog', return_value=None)
    def test_live_command_uses_short_turn_and_requires_completion(self, _):
        app = server.Dashboard()
        app.state.update(source='live',mode='PRESENTATION',armed=True)
        app.picture = lambda: (None, server.time.time())
        commands=[]
        class FakeSerial:
            is_open=True
            def send(self, command):
                commands.append(command)
                responses = {'STATUS':['STATUS:MODE=PRESENTATION,MOTION=0,EMERGENCY=0'],
                             'AI_LEFT_TEST':['AI:ACK:J','AI:MOVE_DONE']}.get(command, [])
                for response in responses:
                    app.rx_seq+=1
                    app.received.append((app.rx_seq,response))
        app.link=FakeSerial()
        app.move('LEFT',{'left':68,'centre':29,'right':35},app.epoch)
        self.assertEqual(commands,['STATUS','AI_LEFT_TEST'])
        self.assertEqual(app.state['stage'],'Step complete · motors stopped')

    @patch.object(server.Dashboard, 'watchdog', return_value=None)
    def test_stale_frame_cancels_before_serial_command(self, _):
        app=server.Dashboard()
        app.state.update(source='live',mode='PRESENTATION',armed=True)
        with self.assertRaises(RuntimeError):
            app.move('LEFT',{'left':68,'centre':29,'right':35},app.epoch)
        self.assertEqual(app.state['last_command'],'—')

    @patch.object(server.Dashboard, 'watchdog', return_value=None)
    def test_heartbeat_loss_blocks_movement(self, _):
        app=server.Dashboard()
        app.arm()
        app.last_heartbeat=server.time.monotonic()-4
        with self.assertRaises(RuntimeError):
            app.move('LEFT',{'left':68,'centre':29,'right':35},app.epoch)

    @patch.object(server.Dashboard, 'watchdog', return_value=None)
    def test_voice_preempts_camera_sequence(self, _):
        app=server.Dashboard()
        app.arm()
        started=server.threading.Event()
        release=server.threading.Event()
        def slow_analysis(execute=False, epoch=None):
            started.set()
            release.wait(2)
            app.valid(epoch)
        app.analyze=slow_analysis
        app.job('auto',{})
        self.assertTrue(started.wait(1))
        app.request_voice('turn left')
        release.set()
        deadline=server.time.monotonic()+2
        while server.time.monotonic()<deadline and app.state['last_command']!='LEFT':
            server.time.sleep(.02)
        self.assertEqual(app.state['last_command'],'LEFT')
        self.assertFalse(app.state['auto'])
        self.assertFalse(app.state['emergency'])

    @patch.object(server.Dashboard, 'watchdog', return_value=None)
    def test_voice_while_controls_off_explains_without_emergency(self, _):
        app=server.Dashboard()
        app.job('voice',{'text':'move forward'})
        deadline=server.time.monotonic()+2
        while server.time.monotonic()<deadline and app.state['busy']:
            server.time.sleep(.02)
        self.assertIn('Controls are OFF', app.state['error'])
        self.assertFalse(app.state['emergency'])
        self.assertEqual(app.state['last_command'],'—')


class RouteTests(unittest.TestCase):
    def test_commas_with_connectors(self):
        expected = [('FORWARD',10),('RIGHT',30),('FORWARD',10)]
        for text in ('forward 10 cm, then right 30 degrees, then forward 10 cm, then stop',
                     'forward 10 centimetre, right 30 degree; and then forward 10 centimetre, stop'):
            self.assertEqual([(s['direction'],s['value']) for s in server.route_plan(text)], expected)

    def test_explicit_angle_and_default(self):
        plan=server.route_plan('move 30 cm forward then take right turn and move 20 cm forward then stop')
        self.assertEqual([(s['direction'],s['value']) for s in plan],[('FORWARD',30),('RIGHT',90),('FORWARD',20)])
        self.assertEqual(server.route_plan('right 30 degrees')[0]['value'],30)
        self.assertEqual(server.route_plan('forward thirty centimetres then right twenty five degrees')[1]['value'],25)

    def test_strict_route_rejection(self):
        for text in ('forward','backward 20 cm','right 200 degrees','forward 101 cm',
                     'forward 1.5 cm','not right','left then stop then right','forward 30 cm then dance',
                     'forward 0 cm','right -30 degrees','stop'):
            with self.assertRaises(ValueError,msg=text): server.route_plan(text)

    def test_twenty_cm_gate_is_separate(self):
        server.route_clearance('FORWARD',{'centre':21,'left':None,'right':10})
        server.route_clearance('RIGHT',{'centre':21,'left':10,'right':21})
        for scan in ({'centre':20,'right':90},{'centre':90,'right':20},{'centre':90,'right':None}):
            with self.assertRaises(RuntimeError): server.route_clearance('RIGHT',scan)
        self.assertEqual(server.decide({'LEFT':100,'CENTER':96,'RIGHT':100}, {'left':50,'centre':21,'right':50})[0],'STOP')

    def test_bounded_estimated_pulses(self):
        self.assertEqual(list(server.route_pulses({'value':30,'unit':'cm'})),[350]*6)
        self.assertEqual(list(server.route_pulses({'value':25,'unit':'deg'})),[120,120,60])

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_preview_never_arms_or_moves(self,_):
        app=server.Dashboard(); app.preview_route('forward 30 cm then right')
        self.assertFalse(app.state['armed']); self.assertEqual(app.state['last_command'],'—')

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_live_route_checks_capability_and_preview(self,_):
        app=server.Dashboard(); app.state.update(source='live',armed=True)
        app.picture=lambda:(None,server.time.time())
        app.preview_route('right')
        with self.assertRaises(RuntimeError): app.run_route('left',app.epoch)
        with self.assertRaisesRegex(RuntimeError,'route-capable'): app.run_route('right',app.epoch)

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_simulated_sequence_finishes_disarmed(self,_):
        app=server.Dashboard(); app.arm(); app.preview_route('forward 5 cm then right 10 degrees then stop')
        app.run_route(app.state['route_text'],app.epoch)
        self.assertFalse(app.state['armed']); self.assertFalse(app.state['emergency'])

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_live_route_pulses_and_cancel(self,_):
        app=server.Dashboard(); app.state.update(source='live',mode='PRESENTATION',armed=True,route_firmware=True)
        app.picture=lambda:(None,server.time.time()); app.scan=lambda:{'left':40,'centre':30,'right':30}
        sent=[]
        class FakeSerial:
            is_open=True
            def send(self,command):
                if command=='STATUS': emit('STATUS:MODE=PRESENTATION,MOTION=0,EMERGENCY=0')
            def send_route_pulse(self,direction,duration):
                sent.append((direction,duration))
                emit('AI:ACK:'+{'FORWARD':'F','LEFT':'L','RIGHT':'R'}[direction]); emit('AI:MOVE_DONE')
        def emit(message):
            app.rx_seq+=1; app.received.append((app.rx_seq,message))
        app.link=FakeSerial(); app.preview_route('forward 5 cm then right 10 degrees')
        app.run_route(app.state['route_text'],app.epoch)
        self.assertEqual(sent,[('FORWARD',350),('RIGHT',120)])
        self.assertFalse(app.state['armed'])
        app.state['armed']=True; epoch=app.epoch; app.stop()
        with self.assertRaises(RuntimeError):app.run_route(app.state['route_text'],epoch)
        self.assertEqual(len(sent),2)


class PassTests(unittest.TestCase):
    def test_distance_parse(self):
        self.assertEqual(server.pass_distance('move forward by 200 cm'),200)
        self.assertEqual(server.pass_distance('forward 50 centimetres'),50)
        self.assertEqual(server.pass_distance('120'),120)
        for bad in ('left 20 cm','forward','forward 900 cm'):
            with self.assertRaises(ValueError): server.pass_distance(bad)

    def test_blocked_rule(self):
        clear={'LEFT':90,'CENTER':90,'RIGHT':90}
        self.assertIsNone(server.pass_blocked(120,clear))
        self.assertIn('ultrasonic',server.pass_blocked(30,clear))
        self.assertIsNone(server.pass_blocked(45,clear))           # side object in the sonar cone no longer forces a turn
        self.assertIn('ultrasonic',server.pass_blocked(45,clear,50))
        self.assertIn('image',server.pass_blocked(200,{'LEFT':90,'CENTER':40,'RIGHT':90}))
        self.assertIsNone(server.pass_blocked(None,clear))
        self.assertIn('image',server.pass_blocked(None,{'LEFT':90,'CENTER':80,'RIGHT':90}))

    def test_turn_side_keeps_direction(self):
        scores={'LEFT':95,'CENTER':10,'RIGHT':40}
        self.assertEqual(server.pass_turn_side(None,scores),'LEFT')
        self.assertEqual(server.pass_turn_side('RIGHT',scores),'RIGHT')

    def test_goal_heading_points_back_to_line(self):
        self.assertEqual(server.pass_goal_heading(0,0,200),0)
        self.assertLess(server.pass_goal_heading(50,20,200),0)     # right of line → aim left
        self.assertGreater(server.pass_goal_heading(50,-20,200),0)
        self.assertEqual(server.pass_goal_heading(195,40,200),-server.PASS_MAX_GOAL_ANGLE)

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_controls_off_does_not_start(self,_):
        app=server.Dashboard()
        with self.assertRaises(server.ControlsOff): app.run_pass('forward 50 cm',app.epoch)

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_simulated_pass_turns_around_and_faces_forward(self,_):
        app=server.Dashboard()
        app.state.update(source='live',mode='PRESENTATION',armed=True,manual_firmware=True,camera_url='x')
        app.camera=object(); app.link=object(); app.picture=lambda:(None,server.time.time())
        app.manual_pulse=lambda direction,duration,epoch: moves.append((direction,duration))
        moves=[]
        # Object straight ahead for the first two checks, then clear.
        looks=iter([(30,{'LEFT':40,'CENTER':20,'RIGHT':95}),(80,{'LEFT':40,'CENTER':50,'RIGHT':95})])
        app.pass_look=lambda: next(looks,(150,{'LEFT':90,'CENTER':90,'RIGHT':90}))
        app.run_pass('forward 150 cm',app.epoch)
        turns=[m for m in moves if m[0]!='FORWARD']
        self.assertEqual(turns[:2],[('RIGHT',120),('RIGHT',120)])  # 10° steps, re-checked between
        self.assertIn('LEFT',[d for d,_ in turns])                 # steered back toward the line
        track=app.state['pass_track']
        self.assertEqual(track['heading'],0)                       # ends facing the original direction
        x,y=track['points'][-1]
        self.assertGreaterEqual(x,150-server.PASS_GOAL_TOLERANCE_CM)
        self.assertLess(abs(y),10)                                 # back on the original line
        self.assertGreater(max(p[1] for p in track['points']),5)   # really went around to the right
        self.assertEqual(len(track['obstacles']),2)
        self.assertIn('Destination reached',app.state['pass_status'])
        self.assertFalse(app.state['emergency'])

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_geometric_bottle_is_passed_before_turning_back(self,_):
        # Bottle 40 cm ahead on the line. The fake sensors only "see" it when the car points at it.
        math=server.math
        app=server.Dashboard()
        app.state.update(source='live',mode='PRESENTATION',armed=True,manual_firmware=True,camera_url='x')
        app.camera=object(); app.link=object(); app.picture=lambda:(None,server.time.time())
        car={'x':0.0,'y':0.0,'h':0}
        bottle=(40.0,0.0)
        def pulse(direction,duration,epoch):
            if direction=='FORWARD':
                cm=duration/70; r=math.radians(car['h'])
                car['x']+=cm*math.cos(r); car['y']+=cm*math.sin(r)
            else:
                car['h']+=(duration/12)*(1 if direction=='RIGHT' else -1)
        def look():
            dx,dy=bottle[0]-car['x'],bottle[1]-car['y']
            dist=math.hypot(dx,dy)
            bearing=math.degrees(math.atan2(dy,dx))-car['h']
            near_line=abs(math.sin(math.radians(bearing))*dist)<12  # bottle within ~car width of the path
            us=round(dist) if abs(bearing)<15 and dist<80 else 150
            centre=15 if near_line and abs(bearing)<60 and dist<70 else 95
            return us,{'LEFT':95,'CENTER':centre,'RIGHT':95}
        app.manual_pulse=pulse; app.pass_look=look
        app.run_pass('forward 150 cm',app.epoch,turn_deg=10,safety_cm=30)
        track=app.state['pass_track']
        closest=min(math.hypot(p[0]-bottle[0],p[1]-bottle[1]) for p in track['points'])
        self.assertGreater(closest,10)                       # never drove into the bottle
        self.assertIn('Destination reached',app.state['pass_status'])
        self.assertLess(abs(car['y']),10)                    # back on the original line
        self.assertEqual(round(car['h']),0)
        # Turned back only after passing the bottle along the line.
        events=[e['message'] for e in app.events]
        self.assertTrue(any('Past the obstacle' in m for m in events))

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_pass_gives_up_when_no_way_found(self,_):
        app=server.Dashboard()
        app.state.update(source='live',mode='PRESENTATION',armed=True,manual_firmware=True,camera_url='x')
        app.camera=object(); app.link=object(); moves=[]; app.picture=lambda:(None,server.time.time())
        app.manual_pulse=lambda direction,duration,epoch: moves.append(direction)
        app.pass_look=lambda:(20,{'LEFT':10,'CENTER':10,'RIGHT':10})
        app.run_pass('forward 100 cm',app.epoch)
        self.assertNotIn('FORWARD',moves)
        self.assertEqual(len(moves),server.PASS_MAX_HEADING//server.PASS_TURN_DEG)
        self.assertEqual(len(set(moves)),1)  # kept turning the same way, never flip-flopped
        self.assertIn('No clear way',app.state['pass_status'])


if __name__ == '__main__': unittest.main()
