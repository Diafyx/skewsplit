"""Read and check the planner input (spec sections 1 and 9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, NoReturn

from skewsplit.core.codecs import INT64_MAX, Codec, codec_for
from skewsplit.core.errors import ErrorCode, SpecError

MAX_PARTITIONS: Final = 10000
_REQUIRED: Final = ("column", "type", "partitions", "null_rows", "mcv", "histogram")
_OPTIONAL: Final = ("scale", "source")


@dataclass(frozen=True)
class PlanInput:
    """A checked planner input, with values as ordinals.

    Attributes:
        column: The column name, unquoted.
        codec: The codec of the column's type.
        partitions: ``N``, the most non-NULL predicates the plan may have.
        null_rows: Rows where the column is NULL.
        mcv: The most common values as ``(ordinal, rows)``, sorted by ordinal.
        bounds: The histogram bounds as ordinals, or empty without a histogram.
        histogram_rows: The rows the histogram covers, 0 without one.
        source: The input's ``source``, copied to the plan.
    """

    column: str
    codec: Codec
    partitions: int
    null_rows: int
    mcv: tuple[tuple[int, int], ...]
    bounds: tuple[int, ...]
    histogram_rows: int
    source: object


def parse_input(data: object) -> PlanInput:
    """Check a decoded JSON input and convert its values to ordinals.

    Raises:
        SpecError: With the code of spec section 9 for the first problem found.
    """
    fields = _shape(data)
    column = fields["column"]
    if not column or "\0" in column:
        _fail("invalid_column", "the column name must not be empty or contain U+0000")
    codec = codec_for(fields["type"], fields.get("scale"))
    partitions = fields["partitions"]
    if not 1 <= partitions <= MAX_PARTITIONS:
        _fail("invalid_partitions", f"partitions is {partitions}; it must be 1 to {MAX_PARTITIONS}")
    histogram = fields["histogram"]
    histogram_rows = histogram["rows"] if histogram else 0
    mcv_rows = [entry["rows"] for entry in fields["mcv"]]
    if fields["null_rows"] < 0 or histogram_rows < 0:
        _fail("invalid_rows", "row counts must not be negative")
    if any(rows < 1 for rows in mcv_rows):
        _fail("invalid_rows", "every common value needs at least 1 row")
    if fields["null_rows"] + sum(mcv_rows) + histogram_rows > INT64_MAX:
        _fail("invalid_rows", f"the total row count exceeds {INT64_MAX}")
    mcv = sorted((codec.parse(entry["value"]), entry["rows"]) for entry in fields["mcv"])
    bounds = tuple(codec.parse(text) for text in histogram["bounds"]) if histogram else ()
    _check_values(codec, mcv, bounds, has_histogram=histogram is not None)
    return PlanInput(
        column=column,
        codec=codec,
        partitions=partitions,
        null_rows=fields["null_rows"],
        mcv=tuple(mcv),
        bounds=bounds,
        histogram_rows=histogram_rows,
        source=fields.get("source"),
    )


def _check_values(
    codec: Codec, mcv: list[tuple[int, int]], bounds: tuple[int, ...], *, has_histogram: bool
) -> None:
    for (value, _), (following, _) in zip(mcv, mcv[1:]):
        if value == following:
            _fail("duplicate_value", f"{codec.text(value)} appears more than once in mcv")
    if has_histogram and len(bounds) < 2:  # noqa: PLR2004 -- a histogram needs two bounds
        _fail("invalid_histogram", "a histogram needs at least 2 bounds")
    for low, high in zip(bounds, bounds[1:]):
        if low > high:
            _fail(
                "invalid_histogram",
                f"bounds {codec.text(low)} and {codec.text(high)} are out of order",
            )
    common = {value for value, _ in mcv}.intersection(bounds)
    if common:
        _fail("invalid_histogram", f"the bound {codec.text(min(common))} is also a common value")


def _shape(data: object) -> dict[str, Any]:
    """Check the JSON shape of section 1: keys, and the JSON type of every value."""
    if not isinstance(data, dict):
        _fail("invalid_input", "the input must be a JSON object")
    _keys(data, _REQUIRED, _OPTIONAL, "the input")
    _expect(data, "column", str)
    _expect(data, "type", str)
    _expect(data, "partitions", int)
    _expect(data, "null_rows", int)
    if "scale" in data:
        _expect(data, "scale", int)
    if not isinstance(data["mcv"], list):
        _fail("invalid_input", "mcv must be a list")
    for entry in data["mcv"]:
        if not isinstance(entry, dict):
            _fail("invalid_input", "each mcv entry must be an object")
        _keys(entry, ("value", "rows"), (), "an mcv entry")
        _expect(entry, "value", str)
        _expect(entry, "rows", int)
    histogram = data["histogram"]
    if histogram is not None:
        if not isinstance(histogram, dict):
            _fail("invalid_input", "histogram must be an object or null")
        _keys(histogram, ("bounds", "rows"), (), "the histogram")
        bounds = histogram["bounds"]
        if not isinstance(bounds, list) or not all(isinstance(b, str) for b in bounds):
            _fail("invalid_input", "histogram bounds must be a list of strings")
        _expect(histogram, "rows", int)
    return data


def _keys(
    obj: dict[str, Any], required: tuple[str, ...], optional: tuple[str, ...], what: str
) -> None:
    missing = [key for key in required if key not in obj]
    if missing:
        _fail("invalid_input", f"{what} is missing {', '.join(missing)}")
    unknown = sorted(str(key) for key in obj if key not in required + optional)
    if unknown:
        _fail("invalid_input", f"{what} has unknown fields: {', '.join(unknown)}")


def _expect(obj: dict[str, Any], key: str, kind: type) -> None:
    # bool is a subclass of int, but JSON true and false are not numbers.
    value = obj[key]
    if not isinstance(value, kind) or isinstance(value, bool):
        _fail("invalid_input", f"{key} must be a JSON {'string' if kind is str else 'integer'}")


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise SpecError(code, message)
