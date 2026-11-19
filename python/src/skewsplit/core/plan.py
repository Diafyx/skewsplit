"""A plan: the predicates, their estimated rows and the warnings (spec section 10)."""

from __future__ import annotations

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

    def to_json(self) -> JSON:
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

    def to_json(self) -> JSON:
        """Return the predicate as in spec section 10."""
        out: JSON = {"kind": self.kind}
        if self.kind == "value":
            out["value"] = self.value
        if self.kind == "range":
            out["lower"] = None if self.lower is None else self.lower.to_json()
            out["upper"] = None if self.upper is None else self.upper.to_json()
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

    def to_json(self) -> JSON:
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
        predicates: The NULL predicate first, then the others in value order.
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
    predicates: tuple[Predicate, ...]
    skew_milli: int | None
    warnings: tuple[PlanWarning, ...]
    source: object = None

    @property
    def planned_partitions(self) -> int:
        """The number of predicates besides the NULL predicate."""
        return len(self.predicates) - 1

    @property
    def skew_score(self) -> float | None:
        """The skew score, a number with at most three decimals."""
        return None if self.skew_milli is None else self.skew_milli / 1000

    def to_json(self) -> JSON:
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
                "predicates": [predicate.to_json() for predicate in self.predicates],
                "skew_score": self.skew_score,
                "warnings": [warning.to_json() for warning in self.warnings],
                "source": self.source,
            }
        )
        return out


def error_json(error: SpecError) -> JSON:
    """Return an error as the JSON object of spec section 10."""
    return {"spec_version": SPEC_VERSION, "error": {"code": error.code, "message": error.message}}
