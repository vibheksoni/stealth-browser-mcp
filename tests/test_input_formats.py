"""Tests for HTML input formatters."""

import datetime
from decimal import Decimal
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp.input_formats import (
    InputFormatError,
    format_color,
    format_date,
    format_datetime_local,
    format_for_input_type,
    format_month,
    format_number,
    format_range,
    format_time,
    format_week,
)


class TestInputFormatError(unittest.TestCase):
    """InputFormatError must be a clear ValueError subclass."""

    def test_subclass_of_value_error(self):
        self.assertTrue(issubclass(InputFormatError, ValueError))
        err = InputFormatError("Test error message")
        self.assertIsInstance(err, ValueError)
        self.assertEqual(str(err), "Test error message")


class TestDateFormat(unittest.TestCase):
    """Table-driven tests for format_date: (input_val, expected_output)."""

    VALID_CASES = [
        # ISO string
        ("2026-03-07", "2026-03-07"),
        ("2026-3-7", "2026-03-07"),
        ("2026/03/07", "2026-03-07"),
        ("2026.03.07", "2026-03-07"),
        # US format M/D/YYYY
        ("3/7/2026", "2026-03-07"),
        ("03/07/2026", "2026-03-07"),
        ("3-7-2026", "2026-03-07"),
        ("12/31/2025", "2025-12-31"),
        # Date strings containing time
        ("2026-03-07 14:30", "2026-03-07"),
        ("2026-03-07T14:30:00", "2026-03-07"),
        ("2026-03-07T14:30:00Z", "2026-03-07"),
        ("3/7/2026 2:30 PM", "2026-03-07"),
        # Textual month formats
        ("March 7, 2026", "2026-03-07"),
        ("Mar 7, 2026", "2026-03-07"),
        ("7 March 2026", "2026-03-07"),
        ("7-Mar-2026", "2026-03-07"),
        # Python objects
        (datetime.date(2026, 3, 7), "2026-03-07"),
        (datetime.datetime(2026, 3, 7, 14, 30, 45), "2026-03-07"),
        # Surrounding whitespace
        ("  2026-03-07  ", "2026-03-07"),
    ]

    INVALID_CASES = [
        "",
        "   ",
        None,
        "not-a-date",
        "2026-02-29",  # 2026 is not a leap year
        "2026-13-01",  # Invalid month
        "2026-04-31",  # April has 30 days
        "2026-00-10",  # Month 0
        12345,
        [2026, 3, 7],
        datetime.time(14, 30),
    ]

    def test_valid_date_conversions(self):
        for i, (inp, expected) in enumerate(self.VALID_CASES):
            with self.subTest(case=i, input=inp):
                self.assertEqual(format_date(inp), expected)

    def test_invalid_date_rejected(self):
        for i, inp in enumerate(self.INVALID_CASES):
            with self.subTest(case=i, input=inp):
                with self.assertRaises(InputFormatError):
                    format_date(inp)


class TestTimeFormat(unittest.TestCase):
    """Table-driven tests for format_time: (input_val, expected_output)."""

    VALID_CASES = [
        # ISO / 24-hour time strings
        ("14:30", "14:30"),
        ("14:30:00", "14:30:00"),
        ("14:30:45", "14:30:45"),
        ("0:00", "00:00"),
        ("23:59", "23:59"),
        ("23:59:59", "23:59:59"),
        # 12-hour AM/PM strings
        ("2:30 PM", "14:30"),
        ("2:30:15 PM", "14:30:15"),
        ("2:30:00 PM", "14:30:00"),
        ("2:30 pm", "14:30"),
        ("12:00 AM", "00:00"),
        ("12:30 AM", "00:30"),
        ("12:00 PM", "12:00"),
        ("12:30 PM", "12:30"),
        ("9:05 AM", "09:05"),
        ("09:05 AM", "09:05"),
        # Strings with date components
        ("2026-03-07 14:30", "14:30"),
        ("2026-03-07T14:30:45", "14:30:45"),
        ("3/7/2026 2:30 PM", "14:30"),
        # Python objects
        (datetime.time(14, 30), "14:30"),
        (datetime.time(14, 30, 45), "14:30:45"),
        (datetime.time(14, 30, 0), "14:30"),
        (datetime.datetime(2026, 3, 7, 14, 30), "14:30"),
        (datetime.datetime(2026, 3, 7, 14, 30, 45), "14:30:45"),
        # Surrounding whitespace
        ("  14:30  ", "14:30"),
    ]

    INVALID_CASES = [
        "",
        "   ",
        None,
        "not-a-time",
        "24:00",
        "25:00",
        "14:60",
        "14:30:60",
        "14",
        "13:00 PM",
        "0:00 PM",
        12345,
        datetime.date(2026, 3, 7),
    ]

    def test_valid_time_conversions(self):
        for i, (inp, expected) in enumerate(self.VALID_CASES):
            with self.subTest(case=i, input=inp):
                self.assertEqual(format_time(inp), expected)

    def test_time_include_seconds_flag(self):
        self.assertEqual(format_time("14:30", include_seconds=True), "14:30:00")
        self.assertEqual(format_time("14:30:45", include_seconds=False), "14:30")

    def test_invalid_time_rejected(self):
        for i, inp in enumerate(self.INVALID_CASES):
            with self.subTest(case=i, input=inp):
                with self.assertRaises(InputFormatError):
                    format_time(inp)


class TestDateTimeLocalFormat(unittest.TestCase):
    """Table-driven tests for format_datetime_local: (input_val, expected_output)."""

    VALID_CASES = [
        # ISO / common strings
        ("2026-03-07 14:30", "2026-03-07T14:30"),
        ("2026-03-07T14:30", "2026-03-07T14:30"),
        ("2026-03-07 14:30:00", "2026-03-07T14:30"),
        ("2026-03-07 14:30:45", "2026-03-07T14:30:45"),
        ("2026-03-07T14:30:45", "2026-03-07T14:30:45"),
        ("3/7/2026 2:30 PM", "2026-03-07T14:30"),
        ("3/7/2026 14:30", "2026-03-07T14:30"),
        ("3/7/2026 2:30:45 PM", "2026-03-07T14:30:45"),
        # Date only (defaults to 00:00)
        ("2026-03-07", "2026-03-07T00:00"),
        ("3/7/2026", "2026-03-07T00:00"),
        # Python objects
        (datetime.datetime(2026, 3, 7, 14, 30), "2026-03-07T14:30"),
        (datetime.datetime(2026, 3, 7, 14, 30, 45), "2026-03-07T14:30:45"),
        (datetime.date(2026, 3, 7), "2026-03-07T00:00"),
    ]

    INVALID_CASES = [
        "",
        "   ",
        None,
        "not-a-datetime",
        "2026-02-29 14:30",
        "2026-03-07 25:00",
        12345,
    ]

    def test_valid_datetime_local_conversions(self):
        for i, (inp, expected) in enumerate(self.VALID_CASES):
            with self.subTest(case=i, input=inp):
                self.assertEqual(format_datetime_local(inp), expected)

    def test_datetime_local_include_seconds_flag(self):
        self.assertEqual(format_datetime_local("2026-03-07 14:30", include_seconds=True), "2026-03-07T14:30:00")
        self.assertEqual(format_datetime_local("2026-03-07 14:30:45", include_seconds=False), "2026-03-07T14:30")

    def test_invalid_datetime_local_rejected(self):
        for i, inp in enumerate(self.INVALID_CASES):
            with self.subTest(case=i, input=inp):
                with self.assertRaises(InputFormatError):
                    format_datetime_local(inp)


class TestMonthFormat(unittest.TestCase):
    """Table-driven tests for format_month: (input_val, expected_output)."""

    VALID_CASES = [
        ("2026-03", "2026-03"),
        ("2026-3", "2026-03"),
        ("2026/03", "2026-03"),
        ("03/2026", "2026-03"),
        ("3/2026", "2026-03"),
        ("March 2026", "2026-03"),
        ("Mar 2026", "2026-03"),
        ("2026 March", "2026-03"),
        ("2026-Mar", "2026-03"),
        ("2026-03-07", "2026-03"),
        ("3/7/2026", "2026-03"),
        (datetime.date(2026, 3, 7), "2026-03"),
        (datetime.datetime(2026, 3, 7, 14, 30), "2026-03"),
    ]

    INVALID_CASES = [
        "",
        "   ",
        None,
        "not-a-month",
        "2026-00",
        "2026-13",
        "13/2026",
        12345,
    ]

    def test_valid_month_conversions(self):
        for i, (inp, expected) in enumerate(self.VALID_CASES):
            with self.subTest(case=i, input=inp):
                self.assertEqual(format_month(inp), expected)

    def test_invalid_month_rejected(self):
        for i, inp in enumerate(self.INVALID_CASES):
            with self.subTest(case=i, input=inp):
                with self.assertRaises(InputFormatError):
                    format_month(inp)


class TestWeekFormat(unittest.TestCase):
    """Table-driven tests for format_week: (input_val, expected_output)."""

    VALID_CASES = [
        ("2026-W10", "2026-W10"),
        ("2026-w10", "2026-W10"),
        ("2026-W05", "2026-W05"),
        ("2026-W5", "2026-W05"),
        ("2026-03-07", "2026-W10"),
        ("3/7/2026", "2026-W10"),
        ("Week 10, 2026", "2026-W10"),
        (datetime.date(2026, 3, 7), "2026-W10"),
        (datetime.datetime(2026, 3, 7, 14, 30), "2026-W10"),
    ]

    INVALID_CASES = [
        "",
        "   ",
        None,
        "not-a-week",
        "2026-W00",
        "2026-W54",
        12345,
    ]

    def test_valid_week_conversions(self):
        for i, (inp, expected) in enumerate(self.VALID_CASES):
            with self.subTest(case=i, input=inp):
                self.assertEqual(format_week(inp), expected)

    def test_invalid_week_rejected(self):
        for i, inp in enumerate(self.INVALID_CASES):
            with self.subTest(case=i, input=inp):
                with self.assertRaises(InputFormatError):
                    format_week(inp)


class TestColorFormat(unittest.TestCase):
    """Table-driven tests for format_color: (input_val, expected_output)."""

    VALID_CASES = [
        # Hex strings
        ("#ff0000", "#ff0000"),
        ("#FF0000", "#ff0000"),
        ("#f00", "#ff0000"),
        ("#F00", "#ff0000"),
        ("#00ff00", "#00ff00"),
        ("#0000ff", "#0000ff"),
        ("#ffffff", "#ffffff"),
        ("#fff", "#ffffff"),
        ("#000000", "#000000"),
        ("#000", "#000000"),
        ("#123456", "#123456"),
        ("#1a2b3c", "#1a2b3c"),
        ("#ff00aa88", "#ff00aa"),
        ("ff0000", "#ff0000"),
        ("f00", "#ff0000"),
        # rgb/rgba strings
        ("rgb(255, 0, 0)", "#ff0000"),
        ("rgb(0, 128, 255)", "#0080ff"),
        ("rgb(0,0,0)", "#000000"),
        ("rgb(255, 255, 255)", "#ffffff"),
        ("rgba(255, 0, 0, 1.0)", "#ff0000"),
        ("rgba(0, 128, 255, 0.5)", "#0080ff"),
        ("rgb(100%, 0%, 0%)", "#ff0000"),
        # Tuples and lists
        ((255, 0, 0), "#ff0000"),
        ([0, 128, 255], "#0080ff"),
        ((255, 0, 0, 255), "#ff0000"),
        # Common CSS names
        ("red", "#ff0000"),
        ("green", "#008000"),
        ("blue", "#0000ff"),
        ("black", "#000000"),
        ("white", "#ffffff"),
        ("yellow", "#ffff00"),
        ("cyan", "#00ffff"),
        ("magenta", "#ff00ff"),
        ("gray", "#808080"),
        ("grey", "#808080"),
        ("silver", "#c0c0c0"),
        ("orange", "#ffa500"),
        ("lime", "#00ff00"),
        ("navy", "#000080"),
        ("teal", "#008080"),
        ("aqua", "#00ffff"),
        ("fuchsia", "#ff00ff"),
    ]

    INVALID_CASES = [
        "",
        "   ",
        None,
        "not-a-color",
        "#12",
        "#1234",
        "#12345",
        "#1234567",
        "#gggggg",
        "rgb(256, 0, 0)",
        "rgb(-1, 0, 0)",
        "rgb(255, 0)",
        (255, 0),
        (256, 0, 0),
        12345,
    ]

    def test_valid_color_conversions(self):
        for i, (inp, expected) in enumerate(self.VALID_CASES):
            with self.subTest(case=i, input=inp):
                self.assertEqual(format_color(inp), expected)

    def test_invalid_color_rejected(self):
        for i, inp in enumerate(self.INVALID_CASES):
            with self.subTest(case=i, input=inp):
                with self.assertRaises(InputFormatError):
                    format_color(inp)


class TestRangeAndNumberFormat(unittest.TestCase):
    """Table-driven tests for format_number and format_range."""

    VALID_CASES = [
        # (value, min, max, step, expected)
        (42, None, None, None, "42"),
        (42.0, None, None, None, "42"),
        (42.5, None, None, None, "42.5"),
        (0, None, None, None, "0"),
        (0.0, None, None, None, "0"),
        (-5, None, None, None, "-5"),
        (-5.25, None, None, None, "-5.25"),
        ("42", None, None, None, "42"),
        ("42.5", None, None, None, "42.5"),
        ("-7", None, None, None, "-7"),
        ("3.14159", None, None, None, "3.14159"),
        (Decimal("42"), None, None, None, "42"),
        # Clamping to min
        (5, 10, None, None, "10"),
        (15, 10, None, None, "15"),
        # Clamping to max
        (25, None, 20, None, "20"),
        (15, None, 20, None, "15"),
        # Clamping to [min, max]
        (5, 10, 20, None, "10"),
        (25, 10, 20, None, "20"),
        (15, 10, 20, None, "15"),
        ("150", "0", "100", None, "100"),
        ("-20", "0", "100", None, "0"),
        # Step snapping
        (37, 0, 100, 10, "40"),
        (32, 0, 100, 10, "30"),
        (35, 0, 100, 10, "40"),
        (150, 0, 100, 10, "100"),
        (-10, 0, 100, 10, "0"),
        (2.3, None, None, 0.5, "2.5"),
        (2.2, None, None, 0.5, "2"),
        (2.0, None, None, 0.5, "2"),
        (1.75, None, None, 0.25, "1.75"),
        (1.8, None, None, 0.25, "1.75"),
    ]

    INVALID_CASES = [
        # (value, min, max, step)
        (None, None, None, None),
        ("", None, None, None),
        ("   ", None, None, None),
        ("not-a-number", None, None, None),
        (True, None, None, None),
        (False, None, None, None),
        (5, 20, 10, None),  # min > max
        (5, None, None, 0),  # step <= 0
        (5, None, None, -1),  # step <= 0
        (5, "bad", None, None),
        (5, None, "bad", None),
        (5, None, None, "bad"),
    ]

    def test_valid_number_conversions(self):
        for i, (val, min_v, max_v, step_v, expected) in enumerate(self.VALID_CASES):
            with self.subTest(case=i, val=val, min=min_v, max=max_v, step=step_v):
                self.assertEqual(format_number(val, min=min_v, max=max_v, step=step_v), expected)
                self.assertEqual(format_range(val, min=min_v, max=max_v, step=step_v), expected)

    def test_invalid_number_rejected(self):
        for i, (val, min_v, max_v, step_v) in enumerate(self.INVALID_CASES):
            with self.subTest(case=i, val=val, min=min_v, max=max_v, step=step_v):
                with self.assertRaises(InputFormatError):
                    format_number(val, min=min_v, max=max_v, step=step_v)


class TestFormatForInputType(unittest.TestCase):
    """Tests for the main format_for_input_type dispatcher function."""

    def test_dispatch_all_types(self):
        self.assertEqual(format_for_input_type("date", "3/7/2026"), "2026-03-07")
        self.assertEqual(format_for_input_type("time", "2:30 PM"), "14:30")
        self.assertEqual(format_for_input_type("datetime-local", "2026-03-07 14:30"), "2026-03-07T14:30")
        self.assertEqual(format_for_input_type("datetime_local", "2026-03-07 14:30"), "2026-03-07T14:30")
        self.assertEqual(format_for_input_type("datetime", "2026-03-07 14:30"), "2026-03-07T14:30")
        self.assertEqual(format_for_input_type("month", "March 2026"), "2026-03")
        self.assertEqual(format_for_input_type("week", "2026-03-07"), "2026-W10")
        self.assertEqual(format_for_input_type("color", "red"), "#ff0000")
        self.assertEqual(format_for_input_type("range", 37, min=0, max=100, step=10), "40")
        self.assertEqual(format_for_input_type("number", 15, min=0, max=10), "10")

    def test_case_insensitive_input_type(self):
        self.assertEqual(format_for_input_type("DATE", "2026-03-07"), "2026-03-07")
        self.assertEqual(format_for_input_type("Time", "14:30"), "14:30")
        self.assertEqual(format_for_input_type("DATETIME-LOCAL", "2026-03-07 14:30"), "2026-03-07T14:30")
        self.assertEqual(format_for_input_type("Color", "BLUE"), "#0000ff")

    def test_unsupported_input_type(self):
        with self.assertRaises(InputFormatError):
            format_for_input_type("unknown_type", "value")
        with self.assertRaises(InputFormatError):
            format_for_input_type(12345, "value")


if __name__ == "__main__":
    unittest.main()
