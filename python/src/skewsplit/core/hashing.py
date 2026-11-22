"""Hash predicates for a column without a usable order (spec section 16).

Each predicate takes one remainder of a hash of the value's text, so the plan
needs only the row counts, works for any type, and gives every partition about
the same number of values. No index can serve these predicates: each
partition's query reads the whole table.
"""

from __future__ import annotations

from typing import Any, Final, NoReturn

from skewsplit.core.codecs import INT64_MAX
from skewsplit.core.errors import ErrorCode, SpecError
from skewsplit.core.inputs import MAX_PARTITIONS
from skewsplit.core.plan import Plan, PlanWarning, Predicate

_REQUIRED: Final = ("column", "type", "partitions", "null_rows", "non_null_rows")
_OPTIONAL: Final = ("source",)


def hash_plan(data: object) -> Plan:
    """Check a decoded JSON input of spec section 16 and plan its hash predicates.

    Raises:
        SpecError: If the input is invalid, with the code of spec section 16.
    """
    fields = _shape(data)
    column, type_name = fields["column"], fields["type"]
    n, null_rows, non_null = fields["partitions"], fields["null_rows"], fields["non_null_rows"]
    if not column or "\0" in column:
        _fail("invalid_column", "the column name must not be empty or contain U+0000")
    if not type_name or "\0" in type_name:
        _fail("unsupported_type", "the type must not be empty or contain U+0000")
    if not 1 <= n <= MAX_PARTITIONS:
        _fail("invalid_partitions", f"partitions must be 1 to {MAX_PARTITIONS}, not {n}")
    if null_rows < 0 or non_null < 0:
        _fail("invalid_rows", "null_rows and non_null_rows must not be negative")
    total = null_rows + non_null
    if total > INT64_MAX:
        _fail("invalid_rows", f"total_rows {total} is above 2^63 - 1")

    quoted = '"' + column.replace('"', '""') + '"'
    remainder = f"((pg_catalog.hashtextextended({quoted}::text, 0) % {n}) + {n}) % {n}"
    predicates = [Predicate("null", f"{quoted} IS NULL", null_rows)]
    predicates += [
        Predicate(
            "hash", f"{remainder} = {i}", (i + 1) * non_null // n - i * non_null // n, remainder=i
        )
        for i in range(n)
    ]
    largest = max(predicate.estimated_rows for predicate in predicates)
    return Plan(
        column=column,
        type_name=type_name,
        scale=None,
        requested_partitions=n,
        null_rows=null_rows,
        total_rows=total,
        parts=tuple(predicates),
        skew_milli=None if total == 0 else (2 * largest * n * 1000 + total) // (2 * total),
        warnings=tuple(_warnings(n, null_rows, total)),
        source=fields.get("source"),
    )


def _warnings(n: int, null_rows: int, total: int) -> list[PlanWarning]:
    """Return the warnings of spec section 16, in its order."""
    warnings = [
        PlanWarning(
            "full_scans",
            f"each of the {n} hash predicates reads the whole table: no index on the column"
            " can serve them, so the read scans the table once per partition",
        )
    ]
    if null_rows * n > total:
        message = f"{null_rows} NULL rows are more than one partition's share"
        warnings.append(PlanWarning("nulls_exceed_share", message, rows=null_rows))
    if total == 0:
        message = "the input shows no rows; the plan still reads rows added later"
        warnings.append(PlanWarning("no_rows", message))
    return warnings


def _shape(data: object) -> dict[str, Any]:
    """Check the keys and the JSON type of every field of the input."""
    if not isinstance(data, dict):
        _fail("invalid_input", "the input must be a JSON object")
    missing = [key for key in _REQUIRED if key not in data]
    if missing:
        _fail("invalid_input", f"the input is missing {', '.join(missing)}")
    unknown = sorted(str(key) for key in data if key not in _REQUIRED + _OPTIONAL)
    if unknown:
        _fail("invalid_input", f"the input has unknown fields: {', '.join(unknown)}")
    for key in ("column", "type"):
        if not isinstance(data[key], str):
            _fail("invalid_input", f"{key} must be a JSON string")
    for key in ("partitions", "null_rows", "non_null_rows"):
        # bool is a subclass of int, but JSON true and false are not numbers.
        if not isinstance(data[key], int) or isinstance(data[key], bool):
            _fail("invalid_input", f"{key} must be a JSON integer")
    return data


def _fail(code: ErrorCode, message: str) -> NoReturn:
    raise SpecError(code, message)
