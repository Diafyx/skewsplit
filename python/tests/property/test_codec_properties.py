"""Properties of the value codecs, checked against independent oracles.

The oracles are the standard library's own date arithmetic, ``Decimal`` and
``int``. None of them shares code with the codecs.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from skewsplit.core.codecs import Codec, codec_for
from skewsplit.core.errors import SpecError

# Naive, like the ordinals: timestamptz values are in UTC.
EPOCH = dt.datetime.combine(dt.date(1970, 1, 1), dt.time())
MICROSECOND = dt.timedelta(microseconds=1)
INT64 = (-(2**63), 2**63 - 1)
FIXED_TYPES = ("int2", "int4", "int8", "date", "timestamp", "timestamptz")
SCALES = st.sampled_from([0, 1, 2, 6, 18, 19, 25, 1000])


@st.composite
def codecs(draw: st.DrawFn) -> Codec:
    type_name = draw(st.sampled_from((*FIXED_TYPES, "numeric")))
    return codec_for(type_name, draw(SCALES) if type_name == "numeric" else None)


@given(st.data())
def test_every_ordinal_round_trips_through_text(data: st.DataObject) -> None:
    codec = data.draw(codecs())
    ordinal = data.draw(st.integers(codec.min_ordinal, codec.max_ordinal))
    text = codec.text(ordinal)
    assert codec.parse(text) == ordinal
    assert codec.literal(ordinal) in {text, f"'{text}'::{codec.type_name}"}


@given(st.data())
def test_order_of_ordinals_is_order_of_values(data: st.DataObject) -> None:
    codec = data.draw(codecs())
    a, b = (data.draw(st.integers(codec.min_ordinal, codec.max_ordinal)) for _ in range(2))
    # Canonical date and timestamp text sorts like the value; numbers sort as Decimal.
    if codec.type_name in {"date", "timestamp", "timestamptz"}:
        assert (a < b) == (codec.text(a) < codec.text(b))
    else:
        assert (a < b) == (Decimal(codec.text(a)) < Decimal(codec.text(b)))


def outcome(codec: Codec, text: str) -> int | str:
    """Return the ordinal of ``text``, or the error code if it does not parse."""
    try:
        return codec.parse(text)
    except SpecError as error:
        return error.code


@given(st.text())
def test_any_text_parses_or_fails_with_a_value_error_code(text: str) -> None:
    for codec in (*(codec_for(t, None) for t in FIXED_TYPES), codec_for("numeric", 2)):
        result = outcome(codec, text)
        if isinstance(result, str):
            assert result in {"invalid_value", "value_out_of_range"}
        else:
            assert codec.min_ordinal <= result <= codec.max_ordinal


@given(st.from_regex(r"-?[0-9]{1,22}", fullmatch=True), st.sampled_from(["int2", "int4", "int8"]))
def test_integers_match_int(text: str, type_name: str) -> None:
    codec = codec_for(type_name, None)
    value = int(text)
    if str(value) != text:  # a leading zero, or -0
        expected: int | str = "invalid_value"
    elif not codec.min_ordinal <= value <= codec.max_ordinal:
        expected = "value_out_of_range"
    else:
        expected = value
    assert outcome(codec, text) == expected


@given(st.from_regex(r"-?(0|[1-9][0-9]{0,22})(\.[0-9]{1,6})?", fullmatch=True), st.integers(0, 4))
def test_numerics_match_decimal(text: str, scale: int) -> None:
    codec = codec_for("numeric", scale)
    value = Decimal(text).scaleb(scale)
    if len(text.partition(".")[2]) > scale or (text.startswith("-") and value == 0):
        expected: int | str = "invalid_value"
    elif not INT64[0] <= value <= INT64[1]:
        expected = "value_out_of_range"
    else:
        expected = int(value)
        assert Decimal(codec.text(expected)) == Decimal(text)
    assert outcome(codec, text) == expected


def timestamp_text(moment: dt.datetime, fraction_digits: int) -> str:
    """Write a datetime in PostgreSQL's ISO form, with a fraction of the given length."""
    text = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d} "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )
    if fraction_digits:
        text += "." + f"{moment.microsecond:06d}"[:fraction_digits]
    return text


@given(st.datetimes(), st.integers(0, 6), st.sampled_from(["timestamp", "timestamptz"]))
def test_timestamps_match_datetime(moment: dt.datetime, digits: int, type_name: str) -> None:
    # A shorter fraction drops trailing digits, so compare with the truncated value.
    kept = int(f"{moment.microsecond:06d}"[:digits].ljust(6, "0"))
    moment = moment.replace(microsecond=kept)
    suffix = "+00" if type_name == "timestamptz" else ""
    codec = codec_for(type_name, None)
    ordinal = codec.parse(timestamp_text(moment, digits) + suffix)
    assert ordinal == (moment - EPOCH) // MICROSECOND
    canonical = timestamp_text(moment, 6).rstrip("0") if kept else timestamp_text(moment, 0)
    assert codec.text(ordinal) == canonical + suffix


@given(st.dates())
def test_dates_match_date(day: dt.date) -> None:
    codec = codec_for("date", None)
    text = f"{day.year:04d}-{day.month:02d}-{day.day:02d}"
    ordinal = codec.parse(text)
    assert ordinal == (day - EPOCH.date()).days
    assert codec.text(ordinal) == text
