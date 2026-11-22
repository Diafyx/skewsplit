"""Balanced JDBC partition predicates for Spark, planned from column statistics.

:func:`plan` plans from a distribution you already have, and :func:`hash_plan`
plans hash predicates for a column without a usable order. To plan from a
PostgreSQL table, see :mod:`skewsplit.postgres`.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import TYPE_CHECKING

from skewsplit.core import hashing, planner
from skewsplit.core.errors import SpecError
from skewsplit.core.plan import Bound, Plan, PlanWarning, Predicate

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Bound",
    "Plan",
    "PlanWarning",
    "Predicate",
    "SpecError",
    "__version__",
    "hash_plan",
    "plan",
]

__version__: str = version("skewsplit")


def plan(distribution: Mapping[str, object], partitions: int) -> Plan:
    """Plan ``partitions`` predicates from a column's distribution.

    Args:
        distribution: The input of spec section 1 without ``partitions``: the
            column's name and type, its NULL rows, its most common values and
            a histogram of the other rows, with values in the type's text form.
        partitions: The most predicates to plan besides the NULL predicate,
            1 to 10000.

    Raises:
        SpecError: If the input is invalid, with the code of spec section 9.

    >>> import skewsplit
    >>> result = skewsplit.plan(
    ...     {
    ...         "column": "customer_id",
    ...         "type": "int4",
    ...         "null_rows": 100,
    ...         "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    ...         "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
    ...     },
    ...     4,
    ... )

    ``predicates`` is the list Spark's JDBC reader takes, one partition each:

    >>> result.predicates[:3]
    ['"customer_id" IS NULL', '"customer_id" < 42', '"customer_id" = 42']

    ``report`` shows the estimated rows of each and the warnings:

    >>> print(result.report)  # doctest: +ELLIPSIS
    customer_id (int4): 1060 estimated rows, 4 of 4 partitions plus one for NULL, skew score 1.887
    rows  predicate
     100  "customer_id" IS NULL
      76  "customer_id" < 42
     500  "customer_id" = 42
     192  "customer_id" > 42 AND "customer_id" < 520
     192  "customer_id" >= 520
    warning dominant_value: 42 holds 500 rows, ...

    ``to_dict`` and ``to_json`` give the output of spec section 10:

    >>> result.to_dict()["skew_score"]
    1.887
    >>> import json
    >>> json.loads(result.to_json()) == result.to_dict()
    True
    """
    if "partitions" in distribution:
        error = SpecError(
            "invalid_input", "give partitions as the argument of plan(), not in the distribution"
        )
        raise error
    return planner.plan({**distribution, "partitions": partitions})


def hash_plan(counts: Mapping[str, object], partitions: int) -> Plan:
    """Plan ``partitions`` hash predicates from a column's row counts (spec section 16).

    For a column whose type has no order to split, such as ``text`` or
    ``uuid``: each predicate takes one remainder of a hash of the value's
    text. No index can serve them, so every partition reads the whole table,
    and the plan always has the warning ``full_scans``.

    Args:
        counts: The input of spec section 16 without ``partitions``: the
            column's name and type, as the database names it, and its NULL
            and non-NULL rows.
        partitions: The number of hash predicates besides the NULL predicate,
            1 to 10000.

    Raises:
        SpecError: If the input is invalid, with the code of spec section 16.

    >>> import skewsplit
    >>> result = skewsplit.hash_plan(
    ...     {"column": "session_id", "type": "uuid", "null_rows": 10, "non_null_rows": 1000}, 4
    ... )
    >>> for predicate in result.predicates[:2]:
    ...     print(predicate)
    "session_id" IS NULL
    ((pg_catalog.hashtextextended("session_id"::text, 0) % 4) + 4) % 4 = 0
    >>> [part.estimated_rows for part in result.parts]
    [10, 250, 250, 250, 250]
    >>> [warning.code for warning in result.warnings]
    ['full_scans']
    """
    if "partitions" in counts:
        error = SpecError(
            "invalid_input", "give partitions as the argument of hash_plan(), not in the counts"
        )
        raise error
    return hashing.hash_plan({**counts, "partitions": partitions})
