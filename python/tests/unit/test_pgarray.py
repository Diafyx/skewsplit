from __future__ import annotations

import pytest

from skewsplit.core.codecs import codec_for
from skewsplit.core.errors import SpecError
from skewsplit.core.pgarray import parse_array, parse_values

# NULL in full-width letters: not ASCII, so not a NULL element.
WIDE_NULL = "".join(chr(ord(letter) + 0xFEE0) for letter in "NULL")


def error_of(text: str) -> SpecError:
    with pytest.raises(SpecError) as caught:
        parse_array(text)
    return caught.value


@pytest.mark.parametrize(
    ("text", "items"),
    [
        ("{}", []),
        ("{a}", ["a"]),
        ('{"a b","c,d","{}","x\\"y","\\\\"}', ["a b", "c,d", "{}", 'x"y', "\\"]),
        ('{"line\nbreak","tab\there"}', ["line\nbreak", "tab\there"]),
        ('{"a\\\nb"}', ["a\nb"]),
        ('{""}', [""]),
        ("{NULL,null,NuLl}", [None, None, None]),
        ('{"NULL"}', ["NULL"]),
        # Only ASCII letters spell NULL: these are ordinary text.
        ("{NULLS,NUL," + WIDE_NULL + "}", ["NULLS", "NUL", WIDE_NULL]),
        ("{é,日本}", ["é", "日本"]),
        ("[-2147483648:-2147483647]={1,2}", ["1", "2"]),
        ("[2147483647:2147483647]={1}", ["1"]),
    ],
)
def test_parse_array(text: str, items: list[str | None]) -> None:
    assert parse_array(text) == items


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("[1:1]{1}", "an array starts with '{'"),
        ('{"a"b}', "elements are separated by ',' and end with '}'"),
        ("{1}}", "nothing may follow the closing '}'"),
        ("[1:2]={1}", "the [L:U] prefix must match the number of elements"),
        ("[-2147483649:-2147483649]={1}", "the [L:U] prefix must match"),
        ("{1,\t2}", "no valid element at offset 3"),
    ],
)
def test_parse_array_errors(text: str, reason: str) -> None:
    error = error_of(text)
    assert error.code == "invalid_array"
    assert error.message.startswith(f"{text!r} is not array text: {reason}")


@pytest.mark.parametrize("char", ["{", "}", '"', "\\", " ", "\t", "\n", "\r", "\v", "\f"])
def test_unquoted_elements_exclude_special_characters(char: str) -> None:
    assert error_of("{a" + char + "b}").code == "invalid_array"
    assert parse_array('{"a' + ("\\" if char in '"\\' else "") + char + 'b"}') == ["a" + char + "b"]


def test_long_text_is_shortened_in_the_message() -> None:
    error = error_of("{" + "9" * 100)
    assert error.message.startswith("'{" + "9" * 56 + "...' is not array text")


def test_parse_values_returns_output_forms() -> None:
    assert parse_values("{0.5,-2}", codec_for("numeric", 3)) == ["0.500", "-2.000"]


def test_parse_values_rejects_a_null_element() -> None:
    with pytest.raises(SpecError, match="pg_stats arrays never hold NULL") as caught:
        parse_values("{1,NULL}", codec_for("int4", None))
    assert caught.value.code == "invalid_array"


@pytest.mark.parametrize(
    ("text", "code"), [('{"1 "}', "invalid_value"), ("{2147483648}", "value_out_of_range")]
)
def test_parse_values_passes_codec_errors_on(text: str, code: str) -> None:
    with pytest.raises(SpecError) as caught:
        parse_values(text, codec_for("int4", None))
    assert caught.value.code == code
