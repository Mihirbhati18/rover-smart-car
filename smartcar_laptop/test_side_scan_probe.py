import unittest

from side_scan_probe import parse_scan_event


class SideScanEventTests(unittest.TestCase):
    def test_valid_distances(self) -> None:
        self.assertEqual(
            parse_scan_event("AI:SCAN:L=93,C=61,R=72"),
            {"left": 93, "centre": 61, "right": 72},
        )

    def test_invalid_echo_is_not_clear(self) -> None:
        self.assertEqual(
            parse_scan_event("AI:SCAN:L=-1,C=42,R=0"),
            {"left": None, "centre": 42, "right": None},
        )

    def test_unrelated_event_is_ignored(self) -> None:
        self.assertIsNone(parse_scan_event("AI:DIST_CM=42"))


if __name__ == "__main__":
    unittest.main()
