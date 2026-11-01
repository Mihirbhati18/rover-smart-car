from __future__ import annotations

import unittest

from command_manager import CommandManager, Direction, parse_voice_command


class FakeArduino:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def send(self, command: str) -> None:
        self.commands.append(command)


class VoiceParserTests(unittest.TestCase):
    def test_common_phrases(self) -> None:
        self.assertEqual(parse_voice_command("move ahead"), Direction.FORWARD)
        self.assertEqual(parse_voice_command("please reverse"), Direction.BACKWARD)
        self.assertEqual(parse_voice_command("turn left"), Direction.LEFT)
        self.assertEqual(parse_voice_command("go right"), Direction.RIGHT)

    def test_stop_wins_over_movement(self) -> None:
        self.assertEqual(
            parse_voice_command("go forward no stop now"),
            Direction.STOP,
        )

    def test_ambiguous_movement_is_refused(self) -> None:
        self.assertIsNone(parse_voice_command("go left and right"))

    def test_negated_movement_is_refused(self) -> None:
        self.assertIsNone(parse_voice_command("do not go forward"))
        self.assertIsNone(parse_voice_command("don't turn left"))


class PriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.arduino = FakeArduino()
        self.manager = CommandManager(self.arduino)

    def test_ai_requires_recovery_mode(self) -> None:
        result = self.manager.send_ai_direction(Direction.LEFT)
        self.assertFalse(result.accepted)
        self.assertEqual(self.arduino.commands, [])

    def test_recovery_sequence(self) -> None:
        self.assertTrue(self.manager.begin_recovery().accepted)
        self.assertTrue(self.manager.send_ai_direction(Direction.LEFT).accepted)
        self.assertTrue(self.manager.finish_recovery().accepted)
        self.assertEqual(
            self.arduino.commands,
            ["AI_START", "AI_LEFT", "AI_RESUME"],
        )

    def test_voice_stop_latches_and_blocks_ai(self) -> None:
        self.manager.begin_recovery()
        stop_result = self.manager.send_voice_text("stop the car")
        blocked_result = self.manager.send_ai_direction(Direction.FORWARD)

        self.assertTrue(stop_result.accepted)
        self.assertTrue(self.manager.emergency_latched)
        self.assertFalse(blocked_result.accepted)
        self.assertEqual(
            self.arduino.commands,
            ["AI_START", "EMERGENCY_STOP"],
        )

    def test_clear_emergency_does_not_move(self) -> None:
        self.manager.send_voice_text("emergency stop")
        result = self.manager.clear_emergency()
        self.assertTrue(result.accepted)
        self.assertFalse(self.manager.emergency_latched)
        self.assertEqual(self.arduino.commands[-1], "CLEAR_EMERGENCY")

    def test_voice_takeover_cancels_recovery(self) -> None:
        self.manager.begin_recovery()
        result = self.manager.send_voice_text("turn left")
        self.assertTrue(result.accepted)
        self.assertFalse(self.manager.recovery_active)
        self.assertFalse(self.manager.send_ai_direction(Direction.RIGHT).accepted)


if __name__ == "__main__":
    unittest.main()
