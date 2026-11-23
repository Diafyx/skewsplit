"""Convert column values between text, integer ordinals and SQL literals.

Every type the planner can split maps each value to an integer, its ordinal,
so that the order of ordinals is the order of the values (spec section 2).
The planner works on ordinals only; a codec reads values from PostgreSQL's
text form and writes them back to text and to SQL literals.

>>> from skewsplit.core.codecs import codec_for
>>> codec = codec_for("date", None)
>>> codec.parse("2026-11-18")
20775
>>> codec.literal(20775)
"'2026-11-18'::date"
"""

from __future__ import annotations

import datetime as dt
import re
from abc import ABC, abstractmethod
from typing import Final

from skewsplit.core.errors import SpecError

INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1
MAX_SCALE: Final = 1000
# The largest int64 has 19 digits, so a value with more is out of range. Checking
# the length first also keeps int() away from very long strings.
_MAX_DIGITS: Final = 19
# How much of a value an error message quotes.
_SHOWN_CHARS: Final = 40
_EPOCH_ORDINAL: Final = dt.date(1970, 1, 1).toordinal()
_DATE_MIN: Final = dt.date.min.toordinal() - _EPOCH_ORDINAL
_DATE_MAX: Final = dt.date.max.toordinal() - _EPOCH_ORDINAL
_US_PER_SECOND: Final = 10**6
_US_PER_DAY: Final = 86400 * _US_PER_SECOND
# What PostgreSQL prints for infinite dates and timestamps and for NaN and
# infinite numerics. None of them has an ordinal.
_SPECIAL_VALUES: Final = frozenset({"infinity", "+infinity", "-infinity", "nan"})

# [0-9] rather than \d, which also matches digits of other scripts.
_INTEGER: Final = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMERIC: Final = re.compile(r"(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?")
_NEGATIVE_ZERO: Final = re.compile(r"-0+(?:\.0+)?")
_DATE: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIMESTAMP: Final = (
    r"([0-9]{4})-([0-9]{2})-([0-9]{2}) ([01][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9])"
    r"(?:\.([0-9]{1,6}))?"
)


class Codec(ABC):
    """The conversions for one column type.

    Attributes:
        type_name: The PostgreSQL type name used in the input and in casts.
        min_ordinal: The smallest ordinal of the type (spec section 2).
        max_ordinal: The largest ordinal of the type.
    """

    def __init__(self, type_name: str, min_ordinal: int, max_ordinal: int, form: str) -> None:
        """Create a codec; ``form`` describes the text form in error messages."""
        self.type_name = type_name
        self.min_ordinal = min_ordinal
        self.max_ordinal = max_ordinal
        self._form = form

    def parse(self, text: str) -> int:
        """Return the ordinal of a value in the type's text form.

        Raises:
            SpecError: ``invalid_value`` if the text is not in the form, or
                ``value_out_of_range`` if its ordinal is outside the type's range.
        """
        ordinal = self._parse(text)
        if ordinal is None:
            raise self._invalid(text)
        if not self.min_ordinal <= ordinal <= self.max_ordinal:
            raise self._out_of_range(_shown(text))
        return ordinal

    def text(self, ordinal: int) -> str:
        """Return the canonical text form of an ordinal.

        Raises:
            SpecError: ``value_out_of_range`` if the ordinal is outside the range.
        """
        if not self.min_ordinal <= ordinal <= self.max_ordinal:
            what = f"ordinal {ordinal}"
            raise self._out_of_range(what)
        return self._format(ordinal)

    def literal(self, ordinal: int) -> str:
        """Return the SQL literal of an ordinal, as written in a predicate."""
        return self.text(ordinal)

    @abstractmethod
    def _parse(self, text: str) -> int | None:
        """Return the ordinal, which may be out of range, or None if the form is wrong."""

    @abstractmethod
    def _format(self, ordinal: int) -> str:
        """Return the text form of an ordinal within the range."""

    def _invalid(self, text: str) -> SpecError:
        lowered = text.strip().lower()
        if lowered in _SPECIAL_VALUES:
            reason = "special values such as infinity and NaN have no ordinal and cannot be planned"
        elif lowered.endswith(" bc"):
            reason = "values before year 1 (BC) are not supported"
        elif _NEGATIVE_ZERO.fullmatch(text):
            reason = "zero is written without a minus sign"
        else:
            reason = f"expected {self._form}"
        message = f"{_shown(text)} is not a valid {self.type_name} value: {reason}"
        return SpecError("invalid_value", message)

    def _out_of_range(self, what: str) -> SpecError:
        low, high = self._format(self.min_ordinal), self._format(self.max_ordinal)
        message = f"{what} is outside the {self.type_name} range, {low} to {high}"
        return SpecError("value_out_of_range", message)


class IntegerCodec(Codec):
    """``int2``, ``int4`` and ``int8``: the ordinal is the value."""

    def __init__(self, type_name: str, bits: int) -> None:
        """Create the codec for a signed integer of ``bits`` bits."""
        half = 2 ** (bits - 1)
        super().__init__(type_name, -half, half - 1, "an integer such as 42 or -5")

    def _parse(self, text: str) -> int | None:
        if not _INTEGER.fullmatch(text) or text == "-0":
            return None
        if len(text.lstrip("-")) > _MAX_DIGITS:
            return INT64_MAX + 1
        return int(text)

    def _format(self, ordinal: int) -> str:
        return str(ordinal)

    def literal(self, ordinal: int) -> str:
        """Return the value bare, except the int8 minimum, which is cast.

        PostgreSQL reads ``-9223372036854775808`` as minus 9223372036854775808,
        which does not fit int8 and so is numeric. A comparison of the column
        with a numeric cannot use the column's B-tree index (spec section 5).
        """
        text = self.text(ordinal)
        return f"'{text}'::int8" if ordinal == INT64_MIN else text


class NumericCodec(Codec):
    """``numeric`` with a declared scale: the ordinal is the value times 10^scale."""

    def __init__(self, scale: int) -> None:
        """Create the codec for ``numeric`` with ``scale`` digits after the point."""
        form = f"a number with at most {scale} digits after the point, such as 12.5"
        super().__init__("numeric", INT64_MIN, INT64_MAX, form)
        self.scale = scale

    def _parse(self, text: str) -> int | None:
        match = _NUMERIC.fullmatch(text)
        if not match:
            return None
        sign, whole, fraction = match.group(1), match.group(2), match.group(3) or ""
        if len(fraction) > self.scale:
            return None
        digits = (whole + fraction.ljust(self.scale, "0")).lstrip("0") or "0"
        if sign and digits == "0":
            return None
        if len(digits) > _MAX_DIGITS:
            return INT64_MAX + 1
        return -int(digits) if sign else int(digits)

    def _format(self, ordinal: int) -> str:
        if self.scale == 0:
            return str(ordinal)
        whole, fraction = divmod(abs(ordinal), 10**self.scale)
        sign = "-" if ordinal < 0 else ""
        return f"{sign}{whole}.{fraction:0{self.scale}d}"


class DateCodec(Codec):
    """``date``: the ordinal is days since 1970-01-01."""

    def __init__(self) -> None:
        """Create the codec for dates from 0001-01-01 to 9999-12-31."""
        super().__init__("date", _DATE_MIN, _DATE_MAX, "a calendar date such as 2026-11-18")

    def _parse(self, text: str) -> int | None:
        match = _DATE.fullmatch(text)
        return None if match is None else _days(*match.groups())

    def _format(self, ordinal: int) -> str:
        return _date_text(ordinal)

    def literal(self, ordinal: int) -> str:
        """Return the value quoted, with a cast: ``'2026-11-18'::date``."""
        return f"'{self.text(ordinal)}'::date"


class TimestampCodec(Codec):
    """``timestamp`` and ``timestamptz``: the ordinal is microseconds since 1970-01-01 (UTC)."""

    def __init__(self, type_name: str, suffix: str) -> None:
        """Create the codec; ``suffix`` follows every value (``+00`` for UTC)."""
        low, high = _DATE_MIN * _US_PER_DAY, (_DATE_MAX + 1) * _US_PER_DAY - 1
        form = f"a timestamp such as 2026-11-18 15:30:00.25{suffix}"
        super().__init__(type_name, low, high, form)
        self._suffix = suffix
        self._pattern = re.compile(_TIMESTAMP + re.escape(suffix))

    def _parse(self, text: str) -> int | None:
        match = self._pattern.fullmatch(text)
        if match is None:
            return None
        year, month, day, hour, minute, second, fraction = match.groups()
        days = _days(year, month, day)
        if days is None:
            return None
        seconds = days * 86400 + int(hour) * 3600 + int(minute) * 60 + int(second)
        return seconds * _US_PER_SECOND + int((fraction or "").ljust(6, "0"))

    def _format(self, ordinal: int) -> str:
        days, micros = divmod(ordinal, _US_PER_DAY)
        seconds, fraction = divmod(micros, _US_PER_SECOND)
        minutes, second = divmod(seconds, 60)
        hour, minute = divmod(minutes, 60)
        text = f"{_date_text(days)} {hour:02d}:{minute:02d}:{second:02d}"
        if fraction:
            text += "." + f"{fraction:06d}".rstrip("0")
        return text + self._suffix

    def literal(self, ordinal: int) -> str:
        """Return the value quoted, with a cast: ``'2026-11-18 15:30:00'::timestamp``."""
        return f"'{self.text(ordinal)}'::{self.type_name}"


def _shown(text: str) -> str:
    """Quote a value for a message, shortened if it is long."""
    return repr(text) if len(text) <= _SHOWN_CHARS else f"{text[:_SHOWN_CHARS]!r}..."


def _days(year: str, month: str, day: str) -> int | None:
    """Return days since 1970-01-01, or None if the date is not in the calendar."""
    try:
        date = dt.date(int(year), int(month), int(day))
    except ValueError:
        return None
    return date.toordinal() - _EPOCH_ORDINAL


def _date_text(days: int) -> str:
    return dt.date.fromordinal(days + _EPOCH_ORDINAL).isoformat()


_FIXED: Final[dict[str, Codec]] = {
    "int2": IntegerCodec("int2", 16),
    "int4": IntegerCodec("int4", 32),
    "int8": IntegerCodec("int8", 64),
    "date": DateCodec(),
    "timestamp": TimestampCodec("timestamp", ""),
    "timestamptz": TimestampCodec("timestamptz", "+00"),
}
TYPE_NAMES: Final = (*_FIXED, "numeric")


def _unsupported_type(message: str) -> SpecError:
    return SpecError("unsupported_type", message)


def codec_for(type_name: str, scale: int | None) -> Codec:
    """Return the codec for a column type and, for ``numeric`` only, its scale.

    Raises:
        SpecError: ``unsupported_type`` if the type is not supported, ``scale``
            is missing for ``numeric`` or given for another type, or ``scale``
            is outside 0 to 1000.
    """
    if type_name == "numeric":
        if scale is None:
            message = "numeric needs its declared scale; unconstrained numeric is not supported"
            raise _unsupported_type(message)
        if not 0 <= scale <= MAX_SCALE:
            message = f"numeric scale {scale} is outside 0 to {MAX_SCALE}"
            raise _unsupported_type(message)
        return NumericCodec(scale)
    codec = _FIXED.get(type_name)
    if codec is None:
        message = f"type {type_name!r} is not supported; expected one of {', '.join(TYPE_NAMES)}"
        raise _unsupported_type(message)
    if scale is not None:
        message = f"scale is only for numeric, not {type_name}"
        raise _unsupported_type(message)
    return codec
