import unittest

from distance_probe import parse_distance_event


class DistanceEventTests(unittest.TestCase):
    def test_valid_distance(self):
        self.assertEqual(parse_distance_event("AI:DIST_CM=42"), 42)

    def test_invalid_echo_is_not_clear(self):
        self.assertIsNone(parse_distance_event("AI:DIST:INVALID"))
        self.assertIsNone(parse_distance_event("AI:DIST_CM=0"))
        self.assertIsNone(parse_distance_event("AI:DIST_CM=999"))

    def test_unrelated_event_is_ignored(self):
        self.assertIsNone(parse_distance_event("AI:ACK:S"))


if __name__ == "__main__":
    unittest.main()
