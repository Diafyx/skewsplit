from __future__ import annotations

import doctest
import json
from pathlib import Path
from typing import Any

import pytest

from skewsplit.core import codecs
from skewsplit.core.codecs import codec_for
from skewsplit.core.errors import SpecError

# The nearest spec/ above, so a copy of the tests one folder deeper, as make
# mutation runs them, finds it too.
SPEC = next(path for path in Path(__file__).parents if (path / "spec").is_dir()) / "spec"
FIXTURES = SPEC / "fixtures"
# Digits of another script, which a regex using \d would accept.
FULLWIDTH_ONE, FULLWIDTH_TWO = chr(0xFF11), chr(0xFF12)


def parse_error(type_name: str, text: str, scale: int | None = None) -> SpecError:
    with pytest.raises(SpecError) as caught:
        codec_for(type_name, scale).parse(text)
    return caught.value


# Spec section 2: the limits of every type, with their text forms.
@pytest.mark.parametrize(
    ("type_name", "scale", "limits", "texts"),
    [
        ("int2", None, (-32768, 32767), ("-32768", "32767")),
        ("int4", None, (-2147483648, 2147483647), ("-2147483648", "2147483647")),
        ("int8", None, (-(2**63), 2**63 - 1), ("-9223372036854775808", "9223372036854775807")),
        ("numeric", 0, (-(2**63), 2**63 - 1), ("-9223372036854775808", "9223372036854775807")),
        ("numeric", 2, (-(2**63), 2**63 - 1), ("-92233720368547758.08", "92233720368547758.07")),
        ("date", None, (-719162, 2932896), ("0001-01-01", "9999-12-31")),
        (
            "timestamp",
            None,
            (-62135596800000000, 253402300799999999),
            ("0001-01-01 00:00:00", "9999-12-31 23:59:59.999999"),
        ),
        (
            "timestamptz",
            None,
            (-62135596800000000, 253402300799999999),
            ("0001-01-01 00:00:00+00", "9999-12-31 23:59:59.999999+00"),
        ),
    ],
)
def test_limits(
    type_name: str, scale: int | None, limits: tuple[int, int], texts: tuple[str, str]
) -> None:
    codec = codec_for(type_name, scale)
    low, high = limits
    assert (codec.min_ordinal, codec.max_ordinal) == limits
    assert (codec.text(low), codec.text(high)) == texts
    assert (codec.parse(texts[0]), codec.parse(texts[1])) == limits
    for outside in (low - 1, high + 1):
        with pytest.raises(SpecError, match="outside") as caught:
            codec.text(outside)
        assert caught.value.code == "value_out_of_range"


# The examples of spec section 2.
@pytest.mark.parametrize(
    ("type_name", "scale", "text", "ordinal", "canonical"),
    [
        ("date", None, "2026-11-18", 20775, "2026-11-18"),
        ("timestamptz", None, "2026-11-18 15:30:00.25+00", 1795015800250000, None),
        (
            "timestamp",
            None,
            "2026-11-18 15:30:00.250000",
            1795015800250000,
            "2026-11-18 15:30:00.25",
        ),
        ("timestamp", None, "1970-01-01 00:00:00.000000", 0, "1970-01-01 00:00:00"),
        ("timestamp", None, "1969-12-31 23:59:59.999999", -1, None),
        ("numeric", 2, "12.5", 1250, "12.50"),
        ("numeric", 2, "-0.07", -7, None),
        ("numeric", 2, "0", 0, "0.00"),
        ("numeric", 0, "-12", -12, None),
        ("numeric", 1000, "0." + "0" * 999 + "1", 1, None),
        ("int4", None, "-5", -5, None),
        ("int2", None, "0", 0, None),
    ],
)
def test_parse_and_text(
    type_name: str, scale: int | None, text: str, ordinal: int, canonical: str | None
) -> None:
    codec = codec_for(type_name, scale)
    assert codec.parse(text) == ordinal
    assert codec.text(ordinal) == (text if canonical is None else canonical)


# The rendering rules of spec section 5.
@pytest.mark.parametrize(
    ("type_name", "scale", "text", "literal"),
    [
        ("int4", None, "42", "42"),
        ("int8", None, "-5", "-5"),
        ("numeric", 2, "12.5", "12.50"),
        ("date", None, "2026-11-18", "'2026-11-18'::date"),
        ("timestamp", None, "2026-11-18 15:30:00", "'2026-11-18 15:30:00'::timestamp"),
        ("timestamptz", None, "2026-11-18 15:30:00+00", "'2026-11-18 15:30:00+00'::timestamptz"),
    ],
)
def test_literal(type_name: str, scale: int | None, text: str, literal: str) -> None:
    codec = codec_for(type_name, scale)
    assert codec.literal(codec.parse(text)) == literal


@pytest.mark.parametrize(
    ("type_name", "scale", "text"),
    [
        *(
            ("int4", None, text)
            for text in (
                "",
                "+1",
                "01",
                "1.0",
                "1e3",
                " 1",
                "1 ",
                "1\n",
                "1_000",
                FULLWIDTH_ONE,
                "--1",
            )
        ),
        *(("numeric", 2, text) for text in ("0.125", ".5", "1.", "+1", "01.5", "1e2", "1,5")),
        ("numeric", 0, "1.0"),
        *(
            ("date", None, text)
            for text in (
                "2026-02-30",
                "2026-13-01",
                "0000-01-01",
                "2026-2-01",
                "20261118",
                "12026-01-01",
                "2026-11-18 00:00:00",
                FULLWIDTH_TWO + "026-11-18",
            )
        ),
        *(
            ("timestamp", None, text)
            for text in (
                "2026-11-18T15:30:00",
                "2026-11-18 15:30",
                "2026-11-18 24:00:00",
                "2026-11-18 15:60:00",
                "2026-11-18 15:30:60",
                "2026-11-18 15:30:00.",
                "2026-11-18 15:30:00.1234567",
                "2026-11-18 15:30:00+00",
                "2026-02-29 00:00:00",
            )
        ),
        *(
            ("timestamptz", None, text)
            for text in (
                "2026-11-18 15:30:00",
                "2026-11-18 15:30:00+01",
                "2026-11-18 15:30:00+00:00",
                "2026-11-18 15:30:00Z",
                "2026-11-18 15:30:00.5 +00",
            )
        ),
    ],
)
def test_text_outside_the_form_is_invalid_value(
    type_name: str, scale: int | None, text: str
) -> None:
    error = parse_error(type_name, text, scale)
    assert error.code == "invalid_value"
    assert "expected" in error.message


@pytest.mark.parametrize(
    ("type_name", "scale", "text", "reason"),
    [
        ("date", None, "infinity", "special values"),
        ("timestamptz", None, "-infinity", "special values"),
        ("numeric", 2, "NaN", "special values"),
        ("date", None, "0044-03-15 BC", "before year 1"),
        ("timestamp", None, "0001-12-31 23:00:00 BC", "before year 1"),
        ("int4", None, "-0", "without a minus sign"),
        ("numeric", 2, "-0.00", "without a minus sign"),
        ("numeric", 2, "-0", "without a minus sign"),
    ],
)
def test_invalid_values_explain_why(
    type_name: str, scale: int | None, text: str, reason: str
) -> None:
    error = parse_error(type_name, text, scale)
    assert error.code == "invalid_value"
    assert reason in error.message


@pytest.mark.parametrize(
    ("type_name", "scale", "text"),
    [
        ("int2", None, "32768"),
        ("int2", None, "-32769"),
        ("int4", None, "2147483648"),
        ("int8", None, "9223372036854775808"),
        ("int8", None, "-9223372036854775809"),
        ("int8", None, "10000000000000000000"),
        ("int8", None, "1" * 5000),
        ("int2", None, "-" + "9" * 30),
        ("numeric", 2, "92233720368547758.08"),
        ("numeric", 2, "-92233720368547758.09"),
        ("numeric", 0, "9" * 5000),
        ("numeric", 1000, "1"),
    ],
)
def test_values_beyond_the_type_are_out_of_range(
    type_name: str, scale: int | None, text: str
) -> None:
    assert parse_error(type_name, text, scale).code == "value_out_of_range"


def test_messages_shorten_long_values() -> None:
    message = parse_error("int8", "1" * 5000).message
    assert message.startswith("'" + "1" * 40 + "'... is outside the int8 range")


@pytest.mark.parametrize(
    ("type_name", "scale", "message"),
    [
        ("text", None, "type 'text' is not supported"),
        ("numeric", None, "needs its declared scale"),
        ("numeric", -1, "outside 0 to 1000"),
        ("numeric", 1001, "outside 0 to 1000"),
        ("int4", 0, "only for numeric"),
        ("date", 2, "only for numeric"),
    ],
)
def test_codec_for_rejects_unsupported_types(
    type_name: str, scale: int | None, message: str
) -> None:
    with pytest.raises(SpecError, match=message) as caught:
        codec_for(type_name, scale)
    assert caught.value.code == "unsupported_type"


def test_numeric_scale_limits_are_supported() -> None:
    assert codec_for("numeric", 0).text(5) == "5"
    assert codec_for("numeric", 1000).text(-1) == "-0." + "0" * 999 + "1"


def test_module_examples() -> None:
    assert doctest.testmod(codecs).failed == 0


def fixture_files(kind: str) -> list[dict[str, Any]]:
    fixtures = []
    for path in sorted(FIXTURES.glob("*.json")):
        fixture: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if ("error" in fixture["expected"]) == (kind == "error"):
            fixtures.append(fixture)
    return fixtures


def input_values(inp: dict[str, Any]) -> list[str]:
    histogram = inp["histogram"]
    return [m["value"] for m in inp["mcv"]] + (histogram["bounds"] if histogram else [])


def test_every_plan_fixture_value_round_trips() -> None:
    for fixture in fixture_files("plan"):
        inp, plan = fixture["input"], fixture["expected"]
        codec = codec_for(inp["type"], inp.get("scale"))
        for text in input_values(inp):
            ordinal = codec.parse(text)
            assert codec.parse(codec.text(ordinal)) == ordinal
        for predicate in plan["predicates"][1:]:
            ends = [predicate.get("lower"), predicate.get("upper"), predicate]
            for value in (end["value"] for end in ends if end and "value" in end):
                assert codec.text(codec.parse(value)) == value
                assert codec.literal(codec.parse(value)) in predicate["sql"]


@pytest.mark.parametrize("code", ["invalid_value", "value_out_of_range"])
def test_value_error_fixtures_fail_with_their_code(code: str) -> None:
    fixtures = [f for f in fixture_files("error") if f["expected"]["error"]["code"] == code]
    assert fixtures
    for fixture in fixtures:
        inp = fixture["input"]
        codec = codec_for(inp["type"], inp.get("scale"))
        codes = [error_code(codec, text) for text in input_values(inp)]
        assert [c for c in codes if c is not None] == [code], fixture["description"]


def error_code(codec: codecs.Codec, text: str) -> str | None:
    try:
        codec.parse(text)
    except SpecError as error:
        return error.code
    return None
