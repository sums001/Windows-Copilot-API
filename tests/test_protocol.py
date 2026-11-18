"""Tests for parsing Copilot chat protocol responses."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from copilot.protocol import TurnParser, _local_timezone_name


class TurnParserTests(unittest.TestCase):
    def test_preserves_error_value_and_message(self):
        parser = TurnParser()

        parser.feed({
            "type": 2,
            "item": {
                "result": {
                    "value": "BackendFailure",
                    "message": "Sorry, I wasn't able to respond.",
                },
            },
        })

        self.assertEqual(
            parser.error,
            "BackendFailure: Sorry, I wasn't able to respond.",
        )

    def test_uses_error_value_when_message_is_missing(self):
        parser = TurnParser()

        parser.feed({
            "type": 2,
            "item": {"result": {"value": "BackendFailure"}},
        })

        self.assertEqual(parser.error, "BackendFailure")


class LocationInfoTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8), "CST"))

    def test_uses_iana_key_when_available(self):
        now = Mock()
        now.tzinfo.key = "Asia/Shanghai"
        with patch("copilot.protocol.get_localzone_name") as localzone:
            self.assertEqual(_local_timezone_name(now), "Asia/Shanghai")
            localzone.assert_not_called()

    def test_uses_tzlocal_for_fixed_offset_timezone(self):
        with patch("copilot.protocol.get_localzone_name", return_value="Asia/Shanghai") as localzone:
            self.assertEqual(_local_timezone_name(self.now), "Asia/Shanghai")
            localzone.assert_called_once_with()

    def test_falls_back_to_utc_when_local_zone_is_unavailable(self):
        with patch("copilot.protocol.get_localzone_name", side_effect=LookupError):
            self.assertEqual(_local_timezone_name(self.now), "UTC")


if __name__ == "__main__":
    unittest.main()
