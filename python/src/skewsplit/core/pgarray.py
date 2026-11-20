"""Read PostgreSQL's array text, as pg_stats shows it (spec section 12)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from skewsplit.core.errors import SpecError

if TYPE_CHECKING:
    from skewsplit.core.codecs import Codec

_INT = r"-?(?:0|[1-9][0-9]*)"
_PREFIX = re.compile(rf"\[({_INT}):({_INT})\]=")
# One character at a time, with disjoint alternatives, so a missing closing
# quote fails in linear time.
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_UNQUOTED = re.compile(r'[^{},"\\ \t\n\r\v\f]+')
_INT4_MIN, _INT4_MAX = -(2**31), 2**31 - 1
# Error messages show at most this many characters of the text.
_SHOWN = 60


def parse_array(text: str) -> list[str | None]:
    """Split one-dimensional array text into its elements; ``None`` is a NULL element.

    Raises:
        SpecError: ``invalid_array`` if the text is not in the format of
            spec section 12.
    """
    prefix = _PREFIX.match(text)
    pos = prefix.end() if prefix else 0
    if not text.startswith("{", pos):
        raise _invalid(text, "an array starts with '{', after an optional [L:U]= prefix")
    pos += 1
    items: list[str | None] = []
    if text.startswith("}", pos):
        pos += 1
    else:
        while True:
            item, pos = _element(text, pos)
            items.append(item)
            if not text.startswith(",", pos):
                break
            pos += 1
        if not text.startswith("}", pos):
            raise _invalid(text, "elements are separated by ',' and end with '}'")
        pos += 1
    if pos != len(text):
        raise _invalid(text, "nothing may follow the closing '}'")
    if prefix:
        lower, upper = int(prefix.group(1)), int(prefix.group(2))
        if not _INT4_MIN <= lower <= upper <= _INT4_MAX or upper - lower + 1 != len(items):
            raise _invalid(text, "the [L:U] prefix must match the number of elements")
    return items


def parse_elements(text: str) -> list[str]:
    """Split array text from pg_stats into its elements, which are never NULL.

    Raises:
        SpecError: ``invalid_array`` for bad array text or a NULL element.
    """
    items = parse_array(text)
    if None in items:
        raise _invalid(text, "pg_stats arrays never hold NULL")
    return cast("list[str]", items)


def parse_values(text: str, codec: Codec) -> list[str]:
    """Read array text as values of the codec's type, in their output form.

    Raises:
        SpecError: ``invalid_array`` for bad array text or a NULL element,
            or the codec's ``invalid_value`` or ``value_out_of_range``.
    """
    return [codec.text(codec.parse(item)) for item in parse_elements(text)]


def _element(text: str, pos: int) -> tuple[str | None, int]:
    """Read the element at ``pos``; return it and the position after it."""
    quoted = _QUOTED.match(text, pos)
    if quoted:
        return _ESCAPE.sub(r"\1", quoted.group(1)), quoted.end()
    unquoted = _UNQUOTED.match(text, pos)
    if not unquoted:
        raise _invalid(text, f"no valid element at offset {pos}")
    word = unquoted.group()
    return (None if word.isascii() and word.upper() == "NULL" else word), unquoted.end()


def _invalid(text: str, reason: str) -> SpecError:
    shown = text if len(text) <= _SHOWN else text[: _SHOWN - 3] + "..."
    return SpecError("invalid_array", f"{shown!r} is not array text: {reason}")
