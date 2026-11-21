"""A plan: the predicates, their estimated rows and the warnings (spec section 10)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from skewsplit.core.errors import SpecError

SPEC_VERSION: Final = "1.0"

PredicateKind = Literal["null", "value", "range"]
WarningCode = Literal[
    "dominant_value",
    "dominant_value_not_isolated",
    "fewer_partitions",
    "nulls_exceed_share",
    "no_rows",
]
JSON = dict[str, object]


@dataclass(frozen=True)
class Bound:
    """One end of a range: ``op`` is ``>=`` or ``>`` below and ``<`` above."""

    op: Literal[">=", ">", "<"]
    value: str

    def to_dict(self) -> JSON:
        """Return the bound as in spec section 10."""
        return {"op": self.op, "value": self.value}


@dataclass(frozen=True)
class Predicate:
    """One predicate of a plan, with the rows it is estimated to read.

    ``value`` is set for a ``value`` predicate; ``lower`` and ``upper`` belong
    to a ``range`` predicate, where ``None`` means the end is open.
    """

    kind: PredicateKind
    sql: str
    estimated_rows: int
    value: str | None = None
    lower: Bound | None = None
    upper: Bound | None = None

    def to_dict(self) -> JSON:
        """Return the predicate as in spec section 10."""
        out: JSON = {"kind": self.kind}
        if self.kind == "value":
            out["value"] = self.value
        if self.kind == "range":
            out["lower"] = None if self.lower is None else self.lower.to_dict()
            out["upper"] = None if self.upper is None else self.upper.to_dict()
        out["sql"] = self.sql
        out["estimated_rows"] = self.estimated_rows
        return out


@dataclass(frozen=True)
class PlanWarning:
    """A warning of spec section 8. Fields that a code does not use are ``None``."""

    code: WarningCode
    message: str
    value: str | None = None
    rows: int | None = None
    requested: int | None = None
    planned: int | None = None

    def to_dict(self) -> JSON:
        """Return the warning with the fields its code defines, then the message."""
        out: JSON = {"code": self.code}
        for key in ("value", "rows", "requested", "planned"):
            field = getattr(self, key)
            if field is not None:
                out[key] = field
        out["message"] = self.message
        return out


@dataclass(frozen=True)
class Plan:
    """The planner's output for one column.

    Attributes:
        column: The column name, unquoted.
        type_name: The column type.
        scale: The ``numeric`` scale, or ``None`` for other types.
        requested_partitions: ``N``.
        null_rows: Rows where the column is NULL.
        total_rows: All rows.
        parts: Each predicate with its estimated rows: the NULL predicate
            first, then the others in value order. These are the
            ``predicates`` of the JSON output.
        skew_milli: The skew score times 1000 (spec section 7), or ``None``
            when there are no rows.
        warnings: In the order of spec section 8.
        source: Where the estimate came from, as the strategy described it:
            for example the statistics' table, freshness and planning time.
    """

    column: str
    type_name: str
    scale: int | None
    requested_partitions: int
    null_rows: int
    total_rows: int
    parts: tuple[Predicate, ...]
    skew_milli: int | None
    warnings: tuple[PlanWarning, ...]
    source: object = None

    @property
    def predicates(self) -> list[str]:
        """The SQL of each predicate, in order: the ``predicates`` of Spark's JDBC reader."""
        return [part.sql for part in self.parts]

    @property
    def planned_partitions(self) -> int:
        """The number of predicates besides the NULL predicate."""
        return len(self.parts) - 1

    @property
    def skew_score(self) -> float | None:
        """The skew score, a number with at most three decimals."""
        return None if self.skew_milli is None else self.skew_milli / 1000

    @property
    def report(self) -> str:
        """The skew report, as text for people.

        A summary line, each predicate with its estimated rows, and then a
        line for each warning: the plan's, followed by those in
        ``source["warnings"]``, which the ``pg_stats`` conversion of spec
        section 13 adds. The layout may change; read :meth:`to_dict` in code.
        """
        kind = self.type_name if self.scale is None else f"{self.type_name}, scale {self.scale}"
        skew = "no skew score"
        if self.skew_milli is not None:
            skew = f"skew score {self.skew_milli / 1000:.3f}"
        summary = (
            f"{self.column} ({kind}): {self.total_rows} estimated rows,"
            f" {self.planned_partitions} of {self.requested_partitions} partitions"
            f" plus one for NULL, {skew}"
        )
        width = max(len("rows"), *(len(str(part.estimated_rows)) for part in self.parts))
        lines = [summary, f"{'rows':>{width}}  predicate"]
        lines.extend(f"{part.estimated_rows:>{width}}  {part.sql}" for part in self.parts)
        warnings: list[tuple[str, str]] = [(w.code, w.message) for w in self.warnings]
        warnings += _source_warnings(self.source)
        lines.extend(f"warning {code}: {message}" for code, message in warnings)
        return "\n".join(lines)

    def to_dict(self) -> JSON:
        """Return the plan as the JSON object of spec section 10."""
        out: JSON = {"spec_version": SPEC_VERSION, "column": self.column, "type": self.type_name}
        if self.scale is not None:
            out["scale"] = self.scale
        out.update(
            {
                "requested_partitions": self.requested_partitions,
                "planned_partitions": self.planned_partitions,
                "rows": {
                    "total": self.total_rows,
                    "null": self.null_rows,
                    "non_null": self.total_rows - self.null_rows,
                },
                "predicates": [part.to_dict() for part in self.parts],
                "skew_score": self.skew_score,
                "warnings": [warning.to_dict() for warning in self.warnings],
                "source": self.source,
            }
        )
        return out

    def to_json(self, *, indent: int | None = None) -> str:
        """Return :meth:`to_dict` as JSON text, with non-ASCII characters as they are.

        Raises:
            TypeError: If ``source`` holds an object JSON has no form for.
            ValueError: If ``source`` holds a NaN or an infinity.
        """
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, allow_nan=False)


def error_dict(error: SpecError) -> JSON:
    """Return an error as the JSON object of spec section 10."""
    return {"spec_version": SPEC_VERSION, "error": {"code": error.code, "message": error.message}}


def _source_warnings(source: object) -> list[tuple[str, str]]:
    warnings = source.get("warnings") if isinstance(source, dict) else None
    if not isinstance(warnings, list):
        return []
    return [
        (warning["code"], warning["message"])
        for warning in warnings
        if isinstance(warning, dict)
        and isinstance(warning.get("code"), str)
        and isinstance(warning.get("message"), str)
    ]
