"""Build the planner input from PostgreSQL's statistics (spec section 13).

All arithmetic is exact, on fractions, so every implementation gets the same
row counts from the same texts.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, Final, NoReturn

from skewsplit.core.codecs import Codec, codec_for
from skewsplit.core.errors import ErrorCode, SpecError
from skewsplit.core.inputs import parse_input
from skewsplit.core.pgarray import parse_elements

_REQUIRED: Final = (
    "column",
    "type",
    "partitions",
    "reltuples",
    "null_frac",
    "most_common_vals",
    "most_common_freqs",
    "histogram_bounds",
    "modified_since_analyze",
)
_OPTIONAL: Final = ("scale",)
_ARRAYS: Final = ("most_common_vals", "most_common_freqs", "histogram_bounds")
# How PostgreSQL prints a non-negative real. The length limit keeps exact
# arithmetic cheap; PostgreSQL's texts are far shorter.
_NUMBER: Final = re.compile(r"(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:e([+-][0-9]{2,3}))?")
_MAX_NUMBER_LENGTH: Final = 40
_TIME_SPECIAL: Final = frozenset({"infinity", "-infinity"})
_SPECIAL: Final = {
    "numeric": frozenset({"NaN", "Infinity", "-Infinity"}),
    "date": _TIME_SPECIAL,
    "timestamp": _TIME_SPECIAL,
    "timestamptz": _TIME_SPECIAL,
}
# More than a tenth of the rows changed: autovacuum_analyze_scale_factor.
_STALE_DIVISOR: Final = 10


def from_pg_stats(data: object) -> dict[str, object]:
    """Convert one ``pg_stats`` row and its table's ``reltuples`` into a planner input.

    Raises:
        SpecError: With the code of spec section 13, or of section 9 if the
            result is not a valid planner input.
    """
    fields = _shape(data)
    modified = fields["modified_since_analyze"]
    if modified is not None and modified < 0:
        _fail("invalid_statistics", "modified_since_analyze must not be negative")
    codec = codec_for(fields["type"], fields.get("scale"))
    special = _SPECIAL.get(codec.type_name, frozenset())
    if not fields["reltuples"]:
        _fail("invalid_statistics", "reltuples needs at least one row count")
    total = sum((_number(text, "reltuples") for text in fields["reltuples"]), Fraction(0))
    rows_estimate = _round(total)
    null_rows = _round(_fraction(fields["null_frac"], "null_frac") * total)
    common = _common_values(fields["most_common_vals"], fields["most_common_freqs"], total)
    unestimated = sum(rows for value, rows in common if value in special)
    mcv = [
        {"value": _value(codec, value), "rows": rows}
        for value, rows in common
        if value not in special
    ]
    histogram: dict[str, object] | None = None
    if fields["histogram_bounds"] is not None:
        common_rows = sum(rows for _, rows in common)
        histogram_rows = max(0, rows_estimate - null_rows - common_rows)
        bounds, kept = _histogram(fields["histogram_bounds"], histogram_rows, special)
        unestimated += histogram_rows - kept
        if len(bounds) >= 2:  # noqa: PLR2004 -- a histogram needs two bounds
            histogram = {"bounds": [_value(codec, bound) for bound in bounds], "rows": kept}
    result: dict[str, object] = {"column": fields["column"], "type": fields["type"]}
    if "scale" in fields:
        result["scale"] = fields["scale"]
    result.update(
        partitions=fields["partitions"],
        null_rows=null_rows,
        mcv=mcv,
        histogram=histogram,
        source={
            "statistics": "pg_stats",
            "rows_estimate": rows_estimate,
            "unestimated_rows": unestimated,
            "modified_since_analyze": modified,
            "warnings": _warnings(rows_estimate, unestimated, modified),
        },
    )
    parse_input(result)
    return result


def _common_values(
    values_text: str | None, freqs_text: str | None, total: Fraction
) -> list[tuple[str, int]]:
    """Return each common value with its rows: at least 1, as section 1 needs."""
    if values_text is None or freqs_text is None:
        if values_text is not None or freqs_text is not None:
            _fail(
                "invalid_statistics",
                "most_common_vals and most_common_freqs must both be null or both be arrays",
            )
        return []
    values, freqs = parse_elements(values_text), parse_elements(freqs_text)
    if len(values) != len(freqs):
        _fail("invalid_statistics", f"{len(values)} common values but {len(freqs)} frequencies")
    return [
        (value, max(1, _round(_fraction(freq, "a frequency") * total)))
        for value, freq in zip(values, freqs)
    ]


def _histogram(text: str, rows: int, special: frozenset[str]) -> tuple[list[str], int]:
    """Return the bounds that are not special and the rows of the buckets between them."""
    bounds = parse_elements(text)
    buckets = len(bounds) - 1
    if buckets < 1:
        _fail(
            "invalid_histogram",
            f"histogram_bounds has {len(bounds)} bounds; a histogram needs at least 2",
        )
    lead = next((i for i, bound in enumerate(bounds) if bound not in special), len(bounds))
    trail = next(
        (i for i, bound in enumerate(reversed(bounds[lead:])) if bound not in special),
        len(bounds) - lead,
    )
    kept = bounds[lead : len(bounds) - trail]
    if any(bound in special for bound in kept):
        _fail("invalid_statistics", "special values can only be the first or last bounds")
    if len(kept) < 2:  # noqa: PLR2004 -- a histogram needs two bounds
        return kept, 0
    return kept, (buckets - trail) * rows // buckets - lead * rows // buckets


def _warnings(rows: int, unestimated: int, modified: int | None) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    if modified is not None and modified * _STALE_DIVISOR > rows:
        warnings.append(
            {
                "code": "stale_statistics",
                "modified_rows": modified,
                "rows": rows,
                "message": (
                    f"{modified} rows changed since the statistics were gathered, more than a"
                    f" tenth of the {rows} rows; run ANALYZE for an accurate plan"
                ),
            }
        )
    if unestimated:
        warnings.append(
            {
                "code": "special_values_left_out",
                "rows": unestimated,
                "message": (
                    f"{unestimated} rows at special values such as infinity, or in histogram"
                    " buckets next to them, are left out of the estimates; the predicates"
                    " still read them"
                ),
            }
        )
    return warnings


def _value(codec: Codec, text: str) -> str:
    return codec.text(codec.parse(text))


def _number(text: str, what: str) -> Fraction:
    """Return the exact value of a decimal text as PostgreSQL prints a real."""
    match = _NUMBER.fullmatch(text) if len(text) <= _MAX_NUMBER_LENGTH else None
    if match is None:
        shown = text[:_MAX_NUMBER_LENGTH]
        _fail("invalid_statistics", f"{what} {shown!r} is not a non-negative decimal number")
    whole, fraction, exponent = match.groups()
    digits = fraction or ""
    value = Fraction(int(whole + digits), 10 ** len(digits))
    return value * Fraction(10) ** int(exponent or 0)


def _fraction(text: str, what: str) -> Fraction:
    value = _number(text, what)
    if value > 1:
        _fail("invalid_statistics", f"{what} {text!r} is above 1")
    return value


def _round(value: Fraction) -> int:
    """``floor(value + 1/2)``."""
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _shape(data: object) -> dict[str, Any]:
    """Check the keys and the JSON type of every field this conversion reads."""
    if not isinstance(data, dict):
        _fail("invalid_input", "the statistics must be a JSON object")
    missing = [key for key in _REQUIRED if key not in data]
    if missing:
        _fail("invalid_input", f"the statistics are missing {', '.join(missing)}")
    unknown = sorted(str(key) for key in data if key not in _REQUIRED + _OPTIONAL)
    if unknown:
        _fail("invalid_input", f"the statistics have unknown fields: {', '.join(unknown)}")
    _check_types(data)
    return data


def _check_types(data: dict[str, Any]) -> None:
    if not isinstance(data["type"], str):
        _fail("invalid_input", "type must be a JSON string")
    if "scale" in data and not _is_integer(data["scale"]):
        _fail("invalid_input", "scale must be a JSON integer")
    reltuples = data["reltuples"]
    if not isinstance(reltuples, list) or not all(isinstance(t, str) for t in reltuples):
        _fail("invalid_input", "reltuples must be a list of strings")
    if not isinstance(data["null_frac"], str):
        _fail("invalid_input", "null_frac must be a JSON string")
    for key in _ARRAYS:
        if data[key] is not None and not isinstance(data[key], str):
            _fail("invalid_input", f"{key} must be a JSON string or null")
    modified = data["modified_since_analyze"]
    if modified is not None and not _is_integer(modified):
        _fail("invalid_input", "modified_since_analyze must be a JSON integer or null")


def _is_integer(value: object) -> bool:
    # bool is a subclass of int, but JSON true and false are not numbers.
    return isinstance(value, int) and not isinstance(value, bool)


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise SpecError(code, message)
