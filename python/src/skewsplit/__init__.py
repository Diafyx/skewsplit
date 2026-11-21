"""Balanced JDBC partition predicates for Spark, planned from column statistics.

:func:`plan` plans from a distribution you already have. To plan from a
PostgreSQL table's statistics, see :mod:`skewsplit.postgres`.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import TYPE_CHECKING

from skewsplit.core import planner
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
