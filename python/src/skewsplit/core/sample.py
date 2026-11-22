"""Build the planner input from a sample of the column (spec section 14).

The sample is each distinct value with the number of sampled rows that have
it, so an adapter can count them in the database and fetch one row per value.
All arithmetic is on integers, so every implementation gets the same input
from the same sample.
"""

from __future__ import annotations

from bisect import bisect_right
from itertools import accumulate
from typing import Any, Final, NoReturn

from skewsplit.core.codecs import INT64_MAX, Codec, codec_for
from skewsplit.core.errors import ErrorCode, SpecError
from skewsplit.core.inputs import parse_input
from skewsplit.core.pgstats import SPECIAL_VALUES

_REQUIRED: Final = (
    "column",
    "type",
    "partitions",
    "rows_estimate",
    "sample_null_rows",
    "sample_values",
)
_OPTIONAL: Final = ("scale",)
# A common value holds at least a thousandth of the ordinary sampled rows, and
# the histogram has at most this many buckets.
RESOLUTION: Final = 1000
# A value sampled once says nothing about how common it is.
_MIN_COMMON_ROWS: Final = 2


def from_sample(data: object) -> dict[str, object]:
    """Convert a sample of a column into a planner input.

    Raises:
        SpecError: With the code of spec section 14, or of section 9 if the
            result is not a valid planner input.
    """
    fields = _shape(data)
    codec = codec_for(fields["type"], fields.get("scale"))
    total, nulls = fields["rows_estimate"], fields["sample_null_rows"]
    entries: list[dict[str, Any]] = fields["sample_values"]
    if total < 0 or nulls < 0:
        _fail("invalid_rows", "rows_estimate and sample_null_rows must not be negative")
    if any(entry["rows"] < 1 for entry in entries):
        _fail("invalid_rows", "every sampled value needs at least 1 row")
    size = nulls + sum(entry["rows"] for entry in entries)
    if total > INT64_MAX or size > INT64_MAX:
        _fail("invalid_rows", f"rows_estimate and the sample size must be at most {INT64_MAX}")
    values, special_rows = _values(codec, entries)

    def scale(count: int) -> int:
        """``count * T / n``, rounded half up; 0 for an empty sample."""
        return (2 * count * total + size) // (2 * size) if size else 0

    ordinary_rows = sum(rows for _, rows in values)
    common: list[tuple[int, int]] = []
    rest: list[tuple[int, int]] = []
    for value, rows in values:
        is_common = rows >= _MIN_COMMON_ROWS and rows * RESOLUTION >= ordinary_rows
        (common if is_common else rest).append((value, rows))
    null_rows, unestimated = scale(nulls), scale(special_rows)
    mcv_rows = [max(1, scale(rows)) for _, rows in common]
    mcv = [{"value": codec.text(value), "rows": rows} for (value, _), rows in zip(common, mcv_rows)]
    histogram: dict[str, object] | None = None
    if rest:
        rows = max(0, total - null_rows - sum(mcv_rows) - unestimated)
        histogram = {"bounds": [codec.text(bound) for bound in _bounds(rest)], "rows": rows}
    result: dict[str, object] = {"column": fields["column"], "type": fields["type"]}
    if "scale" in fields:
        result["scale"] = fields["scale"]
    result.update(
        partitions=fields["partitions"],
        null_rows=null_rows,
        mcv=mcv,
        histogram=histogram,
        source={
            "statistics": "sample",
            "rows_estimate": total,
            "sample_rows": size,
            "unestimated_rows": unestimated,
            "warnings": _warnings(unestimated),
        },
    )
    parse_input(result)
    return result


def _values(codec: Codec, entries: list[dict[str, Any]]) -> tuple[list[tuple[int, int]], int]:
    """Return the ordinary values as ``(ordinal, rows)`` in order, and the special values' rows."""
    special = SPECIAL_VALUES.get(codec.type_name, frozenset())
    ordinary: dict[int, int] = {}
    seen: set[str] = set()
    special_rows = 0
    for entry in entries:
        text, rows = entry["value"], entry["rows"]
        if text in special:
            if text in seen:
                _fail("duplicate_value", f"{text} appears more than once in sample_values")
            seen.add(text)
            special_rows += rows
            continue
        ordinal = codec.parse(text)
        if ordinal in ordinary:
            shown = codec.text(ordinal)
            _fail("duplicate_value", f"{shown} appears more than once in sample_values")
        ordinary[ordinal] = rows
    return sorted(ordinary.items()), special_rows


def _bounds(values: list[tuple[int, int]]) -> list[int]:
    """Return the histogram bounds of the values, each repeated as often as it was sampled.

    With ``h`` values in all and ``m`` buckets, bound ``j`` is the value at
    position ``j * (h - 1) / m``, as ``ANALYZE`` places them.
    """
    ends = list(accumulate(rows for _, rows in values))
    count = ends[-1]
    buckets = min(RESOLUTION, max(1, count - 1))
    # The value at a position is the first whose running total is above it.
    return [values[bisect_right(ends, j * (count - 1) // buckets)][0] for j in range(buckets + 1)]


def _warnings(unestimated: int) -> list[dict[str, object]]:
    if not unestimated:
        return []
    return [
        {
            "code": "special_values_left_out",
            "rows": unestimated,
            "message": (
                f"about {unestimated} rows at special values such as infinity are left out"
                " of the estimates; the predicates still read them"
            ),
        }
    ]


def _shape(data: object) -> dict[str, Any]:
    """Check the keys and the JSON type of every field this conversion reads."""
    if not isinstance(data, dict):
        _fail("invalid_input", "the sample must be a JSON object")
    missing = [key for key in _REQUIRED if key not in data]
    if missing:
        _fail("invalid_input", f"the sample is missing {', '.join(missing)}")
    unknown = sorted(str(key) for key in data if key not in _REQUIRED + _OPTIONAL)
    if unknown:
        _fail("invalid_input", f"the sample has unknown fields: {', '.join(unknown)}")
    if not isinstance(data["type"], str):
        _fail("invalid_input", "type must be a JSON string")
    for key in ("scale", "rows_estimate", "sample_null_rows"):
        if key in data and not _is_integer(data[key]):
            _fail("invalid_input", f"{key} must be a JSON integer")
    entries = data["sample_values"]
    if not isinstance(entries, list) or not all(_is_entry(entry) for entry in entries):
        _fail(
            "invalid_input",
            "sample_values must be a list of objects with a string value and an integer rows",
        )
    return data


def _is_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and set(entry) == {"rows", "value"}
        and isinstance(entry["value"], str)
        and _is_integer(entry["rows"])
    )


def _is_integer(value: object) -> bool:
    # bool is a subclass of int, but JSON true and false are not numbers.
    return isinstance(value, int) and not isinstance(value, bool)


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise SpecError(code, message)
