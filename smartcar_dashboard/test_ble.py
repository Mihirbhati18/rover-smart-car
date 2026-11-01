import unittest
from unittest.mock import patch
import server
from arduino_ble import ArduinoBLE


class BluetoothTests(unittest.TestCase):
    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_camera_free_manual_requires_explicit_enable(self,_):
        app=server.Dashboard()
        app.state.update(source='live',mode='PRESENTATION',manual_firmware=True)
        app.link=type('LiveLink',(),{'is_open':True})()
        app.exchange=lambda *args:'STATUS:MODE=PRESENTATION,MOTION=0,EMERGENCY=0'
        with self.assertRaises(RuntimeError):app.arm()
        self.assertFalse(app.state['armed'])
        app.arm(manual=True)
        app.valid(app.epoch)
        self.assertTrue(app.state['manual_only'])
        with self.assertRaises(RuntimeError):app.analyze(True,app.epoch)
        with self.assertRaises(ValueError):app.run_route('right',app.epoch)
        app.last_heartbeat=server.time.monotonic()-4
        with self.assertRaises(RuntimeError):app.valid(app.epoch)

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_manual_near_or_missing_echo_warns_but_moves_without_scan(self,_):
        for distance_line in ('AI:DIST_CM=10','AI:DIST:INVALID','AI:DIST_CM=111'):
            app=server.Dashboard()
            app.state.update(source='live',mode='PRESENTATION',armed=True,manual_only=True,manual_firmware=True)
            sent=[]
            class FakeLink:
                is_open=True
                def send_manual_pulse(self,direction,duration):
                    sent.append((direction,duration))
                    for message in ('AI:ACK:F','AI:MOVE_DONE'):
                        app.rx_seq+=1
                        app.received.append((app.rx_seq,message))
            app.link=FakeLink()
            commands=[]
            def exchange(command,*args):
                commands.append(command)
                return distance_line if command=='AI_DISTANCE' else 'STATUS:MODE=PRESENTATION,MOTION=0,EMERGENCY=0'
            app.exchange=exchange
            app.scan=lambda: self.fail('Manual movement must not sweep the servo')
            app.picture=lambda: self.fail('Manual movement must not need a camera')
            app.voice('forward',app.epoch)
            self.assertEqual(sent,[('FORWARD',350)])
            self.assertNotIn('AI_SCAN',commands)
            self.assertTrue(app.state['armed'])
            self.assertFalse(app.state['emergency'])
            self.assertTrue(app.state['warning'])

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_manual_bad_phrase_does_not_latch_stop(self,_):
        app=server.Dashboard()
        app.state.update(armed=True,manual_only=True)
        app.job('voice',{'text':'something unknown'})
        deadline=server.time.monotonic()+2
        while app.state['busy'] and server.time.monotonic()<deadline:server.time.sleep(.01)
        self.assertTrue(app.state['armed'])
        self.assertFalse(app.state['emergency'])

    def test_manual_transport_rejects_unbounded_packet(self):
        link=ArduinoBLE()
        try:
            with patch.object(link,'_run') as run:
                for direction,duration in [('FLY',100),('LEFT',0),('FORWARD',351),('BACKWARD',True)]:
                    with self.assertRaises(ValueError):link.send_manual_pulse(direction,duration)
                run.assert_not_called()
        finally:link._loop.close()

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_manual_route_runs_with_invalid_echo_and_keeps_controls_on(self,_):
        app=server.Dashboard()
        app.state.update(source='live',mode='PRESENTATION',armed=True,manual_only=True,manual_firmware=True)
        sent=[]
        class FakeLink:
            is_open=True
            def send_manual_pulse(self,direction,duration):
                sent.append((direction,duration))
                for message in ('AI:ACK:'+{'FORWARD':'F','LEFT':'L','RIGHT':'R','BACKWARD':'B'}[direction],'AI:MOVE_DONE'):
                    app.rx_seq+=1;app.received.append((app.rx_seq,message))
        app.link=FakeLink()
        app.scan=lambda:self.fail('Manual route must not scan sides')
        app.picture=lambda:self.fail('Manual route must not require a camera')
        app.exchange=lambda command,*args:'AI:DIST:INVALID' if command=='AI_DISTANCE' else 'STATUS:MODE=PRESENTATION,MOTION=0,EMERGENCY=0'
        text='forward 10 cm, then right 30 degrees, then reverse 5 cm, then stop'
        app.preview_route(text)
        app.run_route(text,app.epoch)
        self.assertEqual(sent,[('FORWARD',350),('FORWARD',350),('RIGHT',120),('RIGHT',120),('RIGHT',120),('BACKWARD',350)])
        self.assertTrue(app.state['armed'])
        self.assertFalse(app.state['emergency'])
        app.epoch+=1
        with self.assertRaises(RuntimeError):app.run_route(text,app.epoch-1)
        self.assertEqual(len(sent),6)

    def test_manual_route_not_subject_to_camera_mode_size_limits(self):
        text='forward 200 cm then right 270 degrees then reverse 10 cm then left then right then left then forward 5 cm again'
        plan=server.route_plan(text,manual=True)
        self.assertEqual(len(plan),7)
        self.assertEqual(plan[1]['value'],270)
        self.assertEqual(plan[2]['direction'],'BACKWARD')
        with self.assertRaises(ValueError):server.route_plan(text)

    def test_disconnect_is_safe_after_failed_connection_cleanup(self):
        link = ArduinoBLE()
        try:
            link._thread = type('StoppedThread', (), {'is_alive': lambda self: False})()
            with patch.object(link, '_run') as run:
                link.disconnect()
                link.disconnect()
                run.assert_not_called()
        finally:
            link._loop.close()

    def test_fragmented_and_coalesced_notifications(self):
        link = ArduinoBLE()
        try:
            link._notify(None,b'STATUS:MODE=BLUE')
            self.assertIsNone(link.get_event(0))
            link._notify(None,b'TOOTH,MOTION=0,EMERGENCY=0\r\nAI:ACK:S\r\n')
            self.assertEqual(link.get_event(0).text,'STATUS:MODE=BLUETOOTH,MOTION=0,EMERGENCY=0')
            self.assertEqual(link.get_event(0).text,'AI:ACK:S')
        finally:
            link._loop.close()

    def test_no_invalid_or_unbounded_packets(self):
        link = ArduinoBLE()
        try:
            with patch.object(link,'_run') as run:
                for direction,duration in [('BACKWARD',100),('RIGHT',0),('LEFT',351),('FORWARD',True)]:
                    with self.assertRaises(ValueError):link.send_route_pulse(direction,duration)
                with self.assertRaises(ValueError):link.send('anything')
                run.assert_not_called()
        finally:
            link._loop.close()

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_lost_link_shows_off(self,_):
        app=server.Dashboard()
        app.link=type('LostLink',(),{'is_open':False})()
        app.state.update(serial_connected=True,armed=True)
        state=app.snapshot()
        self.assertFalse(state['serial_connected'])
        self.assertFalse(state['armed'])

    @patch.object(server.Dashboard,'watchdog',return_value=None)
    def test_capability_requires_reply(self,_):
        app=server.Dashboard()
        app.link=type('Link',(),{'probe_route':lambda self:None})()
        with patch.object(app,'wait',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):app.verify_ble_firmware()
        self.assertFalse(app.state['route_firmware'])


if __name__ == '__main__':unittest.main()
