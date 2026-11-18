"""Tests for parsing Copilot chat protocol responses."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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

    def test_reads_iana_name_from_localtime(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("copilot.protocol.Path.resolve",
                      return_value=Path("/var/db/timezone/zoneinfo/Asia/Shanghai")), \
                patch("copilot.protocol.ZoneInfo") as zone_info:
            self.assertEqual(_local_timezone_name(self.now), "Asia/Shanghai")
            zone_info.assert_called_once_with("Asia/Shanghai")

    def test_reads_iana_name_from_environment(self):
        with patch.dict(os.environ, {"TZ": ":Asia/Shanghai"}, clear=True), \
                patch("copilot.protocol.ZoneInfo"):
            self.assertEqual(_local_timezone_name(self.now), "Asia/Shanghai")

    def test_rejects_abbreviations(self):
        with patch.dict(os.environ, {"TZ": "CST"}, clear=True), \
                patch("copilot.protocol.Path.resolve", return_value=Path("/etc/localtime")), \
                patch("copilot.protocol.ZoneInfo", side_effect=ValueError):
            self.assertEqual(_local_timezone_name(self.now), "UTC")


if __name__ == "__main__":
    unittest.main()
