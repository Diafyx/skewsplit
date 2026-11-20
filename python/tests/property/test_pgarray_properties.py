"""Fuzz the array text reader of spec section 12.

``array_out`` is a model of how PostgreSQL prints an array (array_out in
src/backend/utils/adt/arrayfuncs.c): it quotes an element that is empty, is
NULL in any case, or holds a special character, and escapes ``"`` and ``\\``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from skewsplit.core.codecs import Codec, codec_for
from skewsplit.core.errors import SpecError
from skewsplit.core.pgarray import parse_array, parse_values

pytestmark = pytest.mark.conformance

SPECIAL = set('{},"\\ \t\n\r\v\f')
TYPES = [("int2", None), ("int4", None), ("int8", None), ("numeric", 2), ("numeric", 0)]
TYPES += [("date", None), ("timestamp", None), ("timestamptz", None)]


def quote(item: str | None) -> str:
    if item is None:
        return "NULL"
    # pg_strcasecmp compares ASCII letters only.
    if item == "" or (item.isascii() and item.upper() == "NULL") or SPECIAL & set(item):
        return '"' + item.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return item


def array_out(items: list[str | None], lower: int) -> str:
    prefix = f"[{lower}:{lower + len(items) - 1}]=" if lower != 1 and items else ""
    return prefix + "{" + ",".join(quote(item) for item in items) + "}"


MAX_ITEMS = 20
ITEMS = st.lists(st.none() | st.text(), max_size=MAX_ITEMS)
# Lower bounds that leave room for MAX_ITEMS elements below the int4 maximum.
LOWER = st.integers(-(2**31), 2**31 - MAX_ITEMS)
# Text built mostly from the characters that matter, so it gets past the first check.
NEAR_ARRAYS = st.text(alphabet='{}[]:=,"\\ -01NULnul\t', max_size=30)


def outcome(text: str, codec: Codec | None = None) -> list[str | None] | list[str] | str:
    """The items or values read from the text, or the error code."""
    try:
        return parse_array(text) if codec is None else parse_values(text, codec)
    except SpecError as error:
        return error.code


@given(ITEMS, LOWER)
def test_printed_arrays_read_back(items: list[str | None], lower: int) -> None:
    assert parse_array(array_out(items, lower)) == items


@given(st.text() | NEAR_ARRAYS)
def test_any_text_reads_or_is_invalid_array(text: str) -> None:
    items = outcome(text)
    assert items == "invalid_array" or isinstance(items, list)


@given(st.data())
def test_printed_values_read_back_in_output_form(data: st.DataObject) -> None:
    type_name, scale = data.draw(st.sampled_from(TYPES))
    codec = codec_for(type_name, scale)
    ordinal = st.integers(codec.min_ordinal, codec.max_ordinal)
    ordinals = data.draw(st.lists(ordinal, max_size=MAX_ITEMS))
    texts = [codec.text(ordinal) for ordinal in ordinals]
    assert parse_values(array_out(list(texts), data.draw(LOWER)), codec) == texts


@given(st.sampled_from(TYPES), st.text() | NEAR_ARRAYS)
def test_any_text_gives_values_or_a_spec_error(
    type_and_scale: tuple[str, int | None], text: str
) -> None:
    codec = codec_for(*type_and_scale)
    values = outcome(text, codec)
    if isinstance(values, str):
        assert values in {"invalid_array", "invalid_value", "value_out_of_range"}
    else:
        assert [codec.text(codec.parse(str(value))) for value in values] == values
