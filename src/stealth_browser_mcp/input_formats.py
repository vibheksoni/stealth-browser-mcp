"""Formatters for HTML form input elements.

Pure functions that normalize ISO strings, datetime objects, numbers, colors,
and common user-provided strings into canonical HTML form input values
according to the HTML5 specification.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional, Tuple


class InputFormatError(ValueError):
    """Raised when an input value cannot be formatted for the target input type."""


# Standard CSS named colors mapping lowercase names to 7-character hex codes (#rrggbb).
CSS_NAMED_COLORS = {
    "aliceblue": "#f0f8ff",
    "antiquewhite": "#faebd7",
    "aqua": "#00ffff",
    "aquamarine": "#7fffd4",
    "azure": "#f0ffff",
    "beige": "#f5f5dc",
    "bisque": "#ffe4c4",
    "black": "#000000",
    "blanchedalmond": "#ffebcd",
    "blue": "#0000ff",
    "blueviolet": "#8a2be2",
    "brown": "#a52a2a",
    "burlywood": "#deb887",
    "cadetblue": "#5f9ea0",
    "chartreuse": "#7fff00",
    "chocolate": "#d2691e",
    "coral": "#ff7f50",
    "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc",
    "crimson": "#dc143c",
    "cyan": "#00ffff",
    "darkblue": "#00008b",
    "darkcyan": "#008b8b",
    "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9",
    "darkgrey": "#a9a9a9",
    "darkgreen": "#006400",
    "darkkhaki": "#bdb76b",
    "darkmagenta": "#8b008b",
    "darkolivegreen": "#556b2f",
    "darkorange": "#ff8c00",
    "darkorchid": "#9932cc",
    "darkred": "#8b0000",
    "darksalmon": "#e9967a",
    "darkseagreen": "#8fbc8f",
    "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f",
    "darkslategrey": "#2f4f4f",
    "darkturquoise": "#00ced1",
    "darkviolet": "#9400d3",
    "deeppink": "#ff1493",
    "deepskyblue": "#00bfff",
    "dimgray": "#696969",
    "dimgrey": "#696969",
    "dodgerblue": "#1e90ff",
    "firebrick": "#b22222",
    "floralwhite": "#fffaf0",
    "forestgreen": "#228b22",
    "fuchsia": "#ff00ff",
    "gainsboro": "#dcdcdc",
    "ghostwhite": "#f8f8ff",
    "gold": "#ffd700",
    "goldenrod": "#daa520",
    "gray": "#808080",
    "grey": "#808080",
    "green": "#008000",
    "greenyellow": "#adff2f",
    "honeydew": "#f0fff0",
    "hotpink": "#ff69b4",
    "indianred": "#cd5c5c",
    "indigo": "#4b0082",
    "ivory": "#fffff0",
    "khaki": "#f0e68c",
    "lavender": "#e6e6fa",
    "lavenderblush": "#fff0f5",
    "lawngreen": "#7cfc00",
    "lemonchiffon": "#fffacd",
    "lightblue": "#add8e6",
    "lightcoral": "#f08080",
    "lightcyan": "#e0ffff",
    "lightgoldenrodyellow": "#fafad2",
    "lightgray": "#d3d3d3",
    "lightgrey": "#d3d3d3",
    "lightgreen": "#90ee90",
    "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a",
    "lightseagreen": "#20b2aa",
    "lightskyblue": "#87cefa",
    "lightslategray": "#778899",
    "lightslategrey": "#778899",
    "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0",
    "lime": "#00ff00",
    "limegreen": "#32cd32",
    "linen": "#faf0e6",
    "magenta": "#ff00ff",
    "maroon": "#800000",
    "mediumaquamarine": "#66cdaa",
    "mediumblue": "#0000cd",
    "mediumorchid": "#ba55d3",
    "mediumpurple": "#9370db",
    "mediumseagreen": "#3cb371",
    "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a",
    "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585",
    "midnightblue": "#191970",
    "mintcream": "#f5fffa",
    "mistyrose": "#ffe4e1",
    "moccasin": "#ffe4e5",
    "navajowhite": "#ffdead",
    "navy": "#000080",
    "oldlace": "#fdf5e6",
    "olive": "#808000",
    "olivedrab": "#6b8e23",
    "orange": "#ffa500",
    "orangered": "#ff4500",
    "orchid": "#da70d6",
    "palegoldenrod": "#eee8aa",
    "palegreen": "#98fb98",
    "paleturquoise": "#afeeee",
    "palevioletred": "#db7093",
    "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9",
    "peru": "#cd853f",
    "pink": "#ffc0cb",
    "plum": "#dda0dd",
    "powderblue": "#b0e0e6",
    "purple": "#800080",
    "rebeccapurple": "#663399",
    "red": "#ff0000",
    "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1",
    "saddlebrown": "#8b4513",
    "salmon": "#fa8072",
    "sandybrown": "#f4a460",
    "seagreen": "#2e8b57",
    "seashell": "#fff5ee",
    "sienna": "#a0522d",
    "silver": "#c0c0c0",
    "skyblue": "#87ceeb",
    "slateblue": "#6a5acd",
    "slategray": "#708090",
    "slategrey": "#708090",
    "snow": "#fffafa",
    "springgreen": "#00ff7f",
    "steelblue": "#4682b4",
    "tan": "#d2b48c",
    "teal": "#008080",
    "thistle": "#d8bfd8",
    "tomato": "#ff6347",
    "turquoise": "#40e0d0",
    "violet": "#ee82ee",
    "wheat": "#f5deb3",
    "white": "#ffffff",
    "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00",
    "yellowgreen": "#9acd32",
}

MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _parse_time(value: Any) -> Tuple[int, int, int, bool]:
    """Parse time from datetime.time, datetime.datetime, or string.

    Returns:
        Tuple[int, int, int, bool]: (hour, minute, second, has_seconds).
    """
    if value is None:
        raise InputFormatError("Time value cannot be None")

    if isinstance(value, datetime.time):
        has_sec = value.second != 0 or value.microsecond != 0
        return value.hour, value.minute, value.second, has_sec

    if isinstance(value, datetime.datetime):
        has_sec = value.second != 0 or value.microsecond != 0
        return value.hour, value.minute, value.second, has_sec

    if not isinstance(value, str):
        raise InputFormatError(f"Expected time string or time object, got {type(value).__name__}")

    s = value.strip()
    if not s:
        raise InputFormatError("Time string cannot be empty")

    m_full = re.search(
        r"(?:[T\s]|^)(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(?:\s*([ap]m))?(?:\s*(?:[zZ]|[+-]\d{2}:?\d{2}))?$",
        s,
        re.IGNORECASE,
    )
    if not m_full:
        raise InputFormatError(f"Cannot parse time from: {value!r}")

    hour_str, min_str, sec_str, meridiem = m_full.groups()
    hour = int(hour_str)
    minute = int(min_str)
    second = int(sec_str) if sec_str is not None else 0
    has_seconds = sec_str is not None

    if meridiem:
        meridiem = meridiem.lower()
        if not (1 <= hour <= 12 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise InputFormatError(f"Time out of range: {value!r}")
        if meridiem == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    else:
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise InputFormatError(f"Time out of range: {value!r}")

    return hour, minute, second, has_seconds


def _parse_date(value: Any) -> datetime.date:
    """Parse a calendar date from date, datetime, or string.

    Returns:
        datetime.date: Parsed calendar date.
    """
    if value is None:
        raise InputFormatError("Date value cannot be None")

    if isinstance(value, datetime.datetime):
        return value.date()

    if isinstance(value, datetime.date):
        return value

    if not isinstance(value, str):
        raise InputFormatError(f"Expected date string or date object, got {type(value).__name__}")

    s = value.strip()
    if not s:
        raise InputFormatError("Date string cannot be empty")

    date_part = s
    if "T" in s:
        date_part = s.split("T", 1)[0].strip()
    else:
        m_time = re.search(
            r"\s+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*[ap]m)?(?:\s*(?:[zZ]|[+-]\d{2}:?\d{2}))?$",
            s,
            re.IGNORECASE,
        )
        if m_time:
            date_part = s[: m_time.start()].strip()

    # 1. ISO format: YYYY-MM-DD or YYYY/MM/DD or YYYY.MM.DD
    m_iso = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", date_part)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return datetime.date(y, m, d)
        except ValueError as e:
            raise InputFormatError(f"Invalid calendar date: {value!r}") from e

    # 2. US format: M/D/YYYY or MM/DD/YYYY or M-D-YYYY
    m_us = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", date_part)
    if m_us:
        m, d, y = int(m_us.group(1)), int(m_us.group(2)), int(m_us.group(3))
        try:
            return datetime.date(y, m, d)
        except ValueError as e:
            raise InputFormatError(f"Invalid calendar date: {value!r}") from e

    # 3. Named month formats: e.g. 'March 7, 2026', 'Mar 7 2026', '7 March 2026', '7-Mar-2026'
    m_named1 = re.match(r"^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", date_part)
    if m_named1:
        month_name = m_named1.group(1).lower()
        d = int(m_named1.group(2))
        y = int(m_named1.group(3))
        if month_name in MONTH_MAP:
            try:
                return datetime.date(y, MONTH_MAP[month_name], d)
            except ValueError as e:
                raise InputFormatError(f"Invalid calendar date: {value!r}") from e

    m_named2 = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?[-/\s]+([A-Za-z]+)[-/\s]+(\d{4})$", date_part)
    if m_named2:
        d = int(m_named2.group(1))
        month_name = m_named2.group(2).lower()
        y = int(m_named2.group(3))
        if month_name in MONTH_MAP:
            try:
                return datetime.date(y, MONTH_MAP[month_name], d)
            except ValueError as e:
                raise InputFormatError(f"Invalid calendar date: {value!r}") from e

    raise InputFormatError(f"Cannot parse date from: {value!r}")


def _parse_datetime(value: Any) -> Tuple[datetime.date, int, int, int, bool]:
    """Parse date and time from datetime, date, or string.

    Returns:
        Tuple[datetime.date, int, int, int, bool]: (date, hour, minute, second, has_seconds).
    """
    if value is None:
        raise InputFormatError("Datetime value cannot be None")

    if isinstance(value, datetime.datetime):
        has_sec = value.second != 0 or value.microsecond != 0
        return value.date(), value.hour, value.minute, value.second, has_sec

    if isinstance(value, datetime.date):
        return value, 0, 0, 0, False

    if not isinstance(value, str):
        raise InputFormatError(f"Expected datetime string or datetime object, got {type(value).__name__}")

    s = value.strip()
    if not s:
        raise InputFormatError("Datetime string cannot be empty")

    m_time = re.search(
        r"[T\s]+(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*[ap]m)?(?:\s*(?:[zZ]|[+-]\d{2}:?\d{2}))?)$",
        s,
        re.IGNORECASE,
    )
    if m_time:
        date_str = s[: m_time.start()].strip()
        time_str = m_time.group(1).strip()
        d = _parse_date(date_str)
        h, m, sec, has_sec = _parse_time(time_str)
        return d, h, m, sec, has_sec
    else:
        d = _parse_date(s)
        return d, 0, 0, 0, False


def format_date(value: Any, **kwargs: Any) -> str:
    """Format value as HTML date string (YYYY-MM-DD).

    Accepts ISO strings, datetime.date/datetime objects, and common inputs
    such as US format '3/7/2026' or 'March 7, 2026'.
    """
    d = _parse_date(value)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def format_time(value: Any, include_seconds: Optional[bool] = None, **kwargs: Any) -> str:
    """Format value as HTML time string (HH:MM or HH:MM:SS).

    Accepts ISO strings, datetime.time/datetime objects, and common inputs
    such as '14:30', '14:30:45', or '2:30 PM'.
    """
    h, m, s, has_sec = _parse_time(value)

    if include_seconds is True:
        return f"{h:02d}:{m:02d}:{s:02d}"
    if include_seconds is False:
        return f"{h:02d}:{m:02d}"

    if has_sec or s != 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}"


def format_datetime_local(value: Any, include_seconds: Optional[bool] = None, **kwargs: Any) -> str:
    """Format value as HTML datetime-local string (YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS).

    Accepts ISO strings, datetime.datetime/date objects, and common inputs
    such as '2026-03-07 14:30' or '3/7/2026 2:30 PM'.
    """
    d, h, m, s, has_sec = _parse_datetime(value)
    date_part = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"

    if include_seconds is True:
        return f"{date_part}T{h:02d}:{m:02d}:{s:02d}"
    if include_seconds is False:
        return f"{date_part}T{h:02d}:{m:02d}"

    if s != 0:
        return f"{date_part}T{h:02d}:{m:02d}:{s:02d}"
    return f"{date_part}T{h:02d}:{m:02d}"


def format_month(value: Any, **kwargs: Any) -> str:
    """Format value as HTML month string (YYYY-MM).

    Accepts ISO month strings ('2026-03'), date objects, and common inputs
    such as 'March 2026' or '3/2026'.
    """
    if value is None:
        raise InputFormatError("Month value cannot be None")

    if isinstance(value, (datetime.date, datetime.datetime)):
        return f"{value.year:04d}-{value.month:02d}"

    if not isinstance(value, str):
        raise InputFormatError(f"Expected month string or date object, got {type(value).__name__}")

    s = value.strip()
    if not s:
        raise InputFormatError("Month string cannot be empty")

    m_ym = re.match(r"^(\d{4})[-/.](\d{1,2})$", s)
    if m_ym:
        y, m = int(m_ym.group(1)), int(m_ym.group(2))
        if 1 <= m <= 12:
            return f"{y:04d}-{m:02d}"
        raise InputFormatError(f"Month must be 1-12, got {m}")

    m_my = re.match(r"^(\d{1,2})[-/](\d{4})$", s)
    if m_my:
        m, y = int(m_my.group(1)), int(m_my.group(2))
        if 1 <= m <= 12:
            return f"{y:04d}-{m:02d}"
        raise InputFormatError(f"Month must be 1-12, got {m}")

    m_named1 = re.match(r"^([A-Za-z]+)[-\s]+(\d{4})$", s)
    if m_named1:
        month_name = m_named1.group(1).lower()
        y = int(m_named1.group(2))
        if month_name in MONTH_MAP:
            return f"{y:04d}-{MONTH_MAP[month_name]:02d}"

    m_named2 = re.match(r"^(\d{4})[-\s]+([A-Za-z]+)$", s)
    if m_named2:
        y = int(m_named2.group(1))
        month_name = m_named2.group(2).lower()
        if month_name in MONTH_MAP:
            return f"{y:04d}-{MONTH_MAP[month_name]:02d}"

    try:
        d = _parse_date(s)
        return f"{d.year:04d}-{d.month:02d}"
    except InputFormatError:
        pass

    raise InputFormatError(f"Cannot parse month from: {value!r}")


def format_week(value: Any, **kwargs: Any) -> str:
    """Format value as HTML week string (YYYY-Www).

    Accepts ISO week strings ('2026-W10'), date objects, and common inputs
    such as date strings or 'Week 10, 2026'.
    """
    if value is None:
        raise InputFormatError("Week value cannot be None")

    if isinstance(value, (datetime.date, datetime.datetime)):
        iso_year, iso_week, _ = value.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"

    if not isinstance(value, str):
        raise InputFormatError(f"Expected week string or date object, got {type(value).__name__}")

    s = value.strip()
    if not s:
        raise InputFormatError("Week string cannot be empty")

    m_w = re.match(r"^(\d{4})-?[wW](\d{1,2})$", s)
    if m_w:
        y = int(m_w.group(1))
        w = int(m_w.group(2))
        if not (1 <= w <= 53):
            raise InputFormatError(f"Week must be 1-53, got {w}")
        try:
            datetime.date.fromisocalendar(y, w, 1)
        except ValueError as e:
            raise InputFormatError(f"Invalid ISO week: {value!r}") from e
        return f"{y:04d}-W{w:02d}"

    m_w_named = re.match(r"^[wW]eek\s+(\d{1,2}),?\s+(\d{4})$", s)
    if m_w_named:
        w = int(m_w_named.group(1))
        y = int(m_w_named.group(2))
        if not (1 <= w <= 53):
            raise InputFormatError(f"Week must be 1-53, got {w}")
        try:
            datetime.date.fromisocalendar(y, w, 1)
        except ValueError as e:
            raise InputFormatError(f"Invalid ISO week: {value!r}") from e
        return f"{y:04d}-W{w:02d}"

    try:
        d = _parse_date(s)
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    except InputFormatError:
        pass

    raise InputFormatError(f"Cannot parse week from: {value!r}")


def format_color(value: Any, **kwargs: Any) -> str:
    """Format value as HTML simple color string (#rrggbb in lowercase).

    Accepts #rrggbb, #rgb shorthand, rgb(r, g, b), rgba(r, g, b, a),
    tuples/lists (r, g, b), and standard CSS named colors (e.g. red, green, blue).
    """
    if value is None:
        raise InputFormatError("Color value cannot be None")

    if isinstance(value, (tuple, list)):
        if len(value) not in (3, 4):
            raise InputFormatError(f"Color sequence must have 3 or 4 components, got {len(value)}")
        try:
            r, g, b = int(value[0]), int(value[1]), int(value[2])
        except (ValueError, TypeError) as e:
            raise InputFormatError(f"Color components must be numbers, got {value!r}") from e
        if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
            raise InputFormatError(f"Color components must be 0-255, got ({r}, {g}, {b})")
        return f"#{r:02x}{g:02x}{b:02x}"

    if not isinstance(value, str):
        raise InputFormatError(f"Expected color string or sequence, got {type(value).__name__}")

    s = value.strip().lower()
    if not s:
        raise InputFormatError("Color string cannot be empty")

    if s in CSS_NAMED_COLORS:
        return CSS_NAMED_COLORS[s]

    if s.startswith("#"):
        hex_part = s[1:]
        if len(hex_part) == 3:
            if not all(c in "0123456789abcdef" for c in hex_part):
                raise InputFormatError(f"Invalid hex characters: {value!r}")
            return f"#{hex_part[0]*2}{hex_part[1]*2}{hex_part[2]*2}"
        elif len(hex_part) == 6:
            if not all(c in "0123456789abcdef" for c in hex_part):
                raise InputFormatError(f"Invalid hex characters: {value!r}")
            return f"#{hex_part}"
        elif len(hex_part) == 8:
            if not all(c in "0123456789abcdef" for c in hex_part):
                raise InputFormatError(f"Invalid hex characters: {value!r}")
            return f"#{hex_part[:6]}"
        else:
            raise InputFormatError(f"Hex color must be 3 or 6 hex digits, got {value!r}")

    if (len(s) == 3 or len(s) == 6) and all(c in "0123456789abcdef" for c in s):
        if len(s) == 3:
            return f"#{s[0]*2}{s[1]*2}{s[2]*2}"
        return f"#{s}"

    m_rgb = re.match(
        r"^rgba?\(\s*(\d+%?)\s*,\s*(\d+%?)\s*,\s*(\d+%?)(?:\s*,\s*[^)]+)?\)$",
        s,
    )
    if m_rgb:
        nums = []
        for p in (m_rgb.group(1), m_rgb.group(2), m_rgb.group(3)):
            if p.endswith("%"):
                pct = float(p[:-1])
                if not (0 <= pct <= 100):
                    raise InputFormatError(f"RGB percentage must be 0-100%, got {p}")
                nums.append(round(pct * 255 / 100))
            else:
                n = int(p)
                if not (0 <= n <= 255):
                    raise InputFormatError(f"RGB component must be 0-255, got {p}")
                nums.append(n)
        return f"#{nums[0]:02x}{nums[1]:02x}{nums[2]:02x}"

    raise InputFormatError(f"Cannot parse color from: {value!r}")


def _to_decimal(val: Any, name: str) -> Decimal:
    if val is None:
        raise InputFormatError(f"{name} cannot be None")
    if isinstance(val, bool):
        raise InputFormatError(f"Boolean value is not a valid {name}")
    if isinstance(val, (int, Decimal)):
        d = Decimal(val)
        if d.is_nan() or d.is_infinite():
            raise InputFormatError(f"{name} cannot be NaN or Infinity")
        return d
    if isinstance(val, float):
        d = Decimal(str(val))
        if d.is_nan() or d.is_infinite():
            raise InputFormatError(f"{name} cannot be NaN or Infinity")
        return d
    if isinstance(val, str):
        s = val.strip()
        if not s:
            raise InputFormatError(f"{name} string cannot be empty")
        try:
            d = Decimal(s)
            if d.is_nan() or d.is_infinite():
                raise InputFormatError(f"{name} cannot be NaN or Infinity")
            return d
        except InvalidOperation as e:
            raise InputFormatError(f"Invalid {name} numeric value: {val!r}") from e
    try:
        d = Decimal(str(val))
        if d.is_nan() or d.is_infinite():
            raise InputFormatError(f"{name} cannot be NaN or Infinity")
        return d
    except Exception as e:
        raise InputFormatError(f"Cannot convert {name} to number: {val!r}") from e


def _format_decimal(d: Decimal) -> str:
    """Format Decimal into clean numeric string without scientific notation or trailing zeros."""
    if d == 0:
        return "0"
    s = f"{d:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def format_number(
    value: Any,
    min: Optional[Any] = None,
    max: Optional[Any] = None,
    step: Optional[Any] = None,
    *,
    min_value: Optional[Any] = None,
    max_value: Optional[Any] = None,
    **kwargs: Any,
) -> str:
    """Format numeric value for range/number inputs, with optional clamping to min/max and snapping to step."""
    val_dec = _to_decimal(value, "value")

    if min is None and min_value is not None:
        min = min_value
    if max is None and max_value is not None:
        max = max_value

    min_dec = _to_decimal(min, "min") if min is not None else None
    max_dec = _to_decimal(max, "max") if max is not None else None

    if min_dec is not None and max_dec is not None and min_dec > max_dec:
        raise InputFormatError(f"min ({min}) cannot be greater than max ({max})")

    step_dec = None
    if step is not None:
        step_dec = _to_decimal(step, "step")
        if step_dec <= 0:
            raise InputFormatError(f"step must be positive, got {step}")

    if min_dec is not None and val_dec < min_dec:
        val_dec = min_dec
    if max_dec is not None and val_dec > max_dec:
        val_dec = max_dec

    if step_dec is not None:
        base = min_dec if min_dec is not None else Decimal(0)
        ratio = (val_dec - base) / step_dec
        steps = ratio.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        val_dec = base + steps * step_dec
        if max_dec is not None and val_dec > max_dec:
            val_dec -= step_dec
        if min_dec is not None and val_dec < min_dec:
            val_dec += step_dec

    return _format_decimal(val_dec)


def format_range(
    value: Any,
    min: Optional[Any] = None,
    max: Optional[Any] = None,
    step: Optional[Any] = None,
    **kwargs: Any,
) -> str:
    """Format value for input type='range' (alias for format_number)."""
    return format_number(value, min=min, max=max, step=step, **kwargs)


def format_for_input_type(
    input_type: str,
    value: Any,
    min: Optional[Any] = None,
    max: Optional[Any] = None,
    step: Optional[Any] = None,
    **kwargs: Any,
) -> str:
    """Format value for the specified HTML input type.

    Supported input types:
      - 'date': YYYY-MM-DD
      - 'time': HH:MM or HH:MM:SS
      - 'datetime-local' (or 'datetime'): YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS
      - 'month': YYYY-MM
      - 'week': YYYY-Www
      - 'color': #rrggbb lowercase
      - 'range': numeric string clamped to min/max/step
      - 'number': numeric string clamped to min/max/step

    Raises:
        InputFormatError: If the input_type is unsupported or the value cannot be formatted.
    """
    if not isinstance(input_type, str):
        raise InputFormatError(f"input_type must be a string, got {type(input_type).__name__}")

    norm_type = input_type.lower().strip().replace("_", "-")

    if norm_type == "date":
        return format_date(value, **kwargs)
    elif norm_type == "time":
        return format_time(value, **kwargs)
    elif norm_type in ("datetime-local", "datetime"):
        return format_datetime_local(value, **kwargs)
    elif norm_type == "month":
        return format_month(value, **kwargs)
    elif norm_type == "week":
        return format_week(value, **kwargs)
    elif norm_type == "color":
        return format_color(value, **kwargs)
    elif norm_type in ("range", "number"):
        return format_number(value, min=min, max=max, step=step, **kwargs)
    else:
        raise InputFormatError(f"Unsupported input type: {input_type!r}")
