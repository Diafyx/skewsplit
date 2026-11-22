"""Plan from a table's statistics, a sample of it or every row, or plan hash predicates.

``statistics`` reads ``pg_stats`` and never the table (spec section 13).
``sample`` reads a sample of the table with ``TABLESAMPLE`` (section 14), for
when the statistics are missing or out of date. ``exact`` reads every row
(section 15), for when accuracy matters more than planning time.
``method="hash"`` plans hash predicates for a column of any type (section 16).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Literal

from skewsplit.postgres.exact import exact_distribution, plan_from_exact
from skewsplit.postgres.hashing import plan_from_hash
from skewsplit.postgres.sample import DEFAULT_SAMPLE_ROWS, plan_from_sample, sample_distribution
from skewsplit.postgres.statistics import distribution as statistics_distribution
from skewsplit.postgres.statistics import plan_from_statistics

if TYPE_CHECKING:
    import psycopg

    from skewsplit.core.plan import JSON, Plan

Strategy = Literal["statistics", "sample", "exact"]
STRATEGIES: Final[tuple[Strategy, ...]] = ("statistics", "sample", "exact")
Method = Literal["range", "hash"]
METHODS: Final[tuple[Method, ...]] = ("range", "hash")


def distribution(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    *,
    strategy: Strategy = "statistics",
    sample_rows: int | None = None,
    max_sample_rows: int | None = None,
    seed: int | None = None,
    statement_timeout_ms: int | None = None,
) -> JSON:
    """Return a column's distribution: the input of spec section 1 without ``partitions``.

    It is what :func:`skewsplit.plan` takes, and its ``source`` says where it
    came from.

    Args:
        database: A libpq connection string, or an open connection, which is
            left open and with its settings and transaction as they were.
        table: The table as SQL names it, as for :func:`find_column`.
        column: The column's exact name, not quoted.
        strategy: ``statistics`` to read ``pg_stats``, ``sample`` to read a
            sample of the table, or ``exact`` to read every row: it scans the
            whole table three times and sorts the column.
        sample_rows: For ``sample``: about how many rows to sample, 30000 by
            default.
        max_sample_rows: For ``sample``: the most rows the sample may read,
            10 times ``sample_rows`` by default.
        seed: For ``sample``: a ``REPEATABLE`` seed, so that the same table
            gives the same sample.
        statement_timeout_ms: A ``statement_timeout`` for each query that
            reads the statistics or the table, in milliseconds.

    Raises:
        CatalogError: If the column cannot be split, or has no statistics
            for ``statistics``.
        SpecError: If what was read does not convert into a valid input.
        ValueError: If the strategy is unknown, or an option is out of range
            or given for the other strategy.
        psycopg.Error: If a query fails or was cancelled by the timeout.
    """
    chosen = _strategy(strategy, sample_rows, max_sample_rows, seed)
    if chosen == "exact":
        return exact_distribution(
            database, table, column, statement_timeout_ms=statement_timeout_ms
        )
    if chosen == "sample":
        return sample_distribution(
            database,
            table,
            column,
            sample_rows=DEFAULT_SAMPLE_ROWS if sample_rows is None else sample_rows,
            max_sample_rows=max_sample_rows,
            seed=seed,
            statement_timeout_ms=statement_timeout_ms,
        )
    return statistics_distribution(
        database, table, column, statement_timeout_ms=statement_timeout_ms
    )


def plan(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    partitions: int,
    *,
    method: Method = "range",
    strategy: Strategy | None = None,
    sample_rows: int | None = None,
    max_sample_rows: int | None = None,
    seed: int | None = None,
    statement_timeout_ms: int | None = None,
) -> Plan:
    """Plan ``partitions`` predicates for a column.

    The arguments are those of :func:`distribution`, and ``partitions`` is
    the number of predicates to plan besides the NULL predicate. The plan's
    ``source`` also has ``planning_ms``: the time spent finding the column,
    reading its distribution and planning.

    ``method="hash"`` plans hash predicates instead of ranges (spec section
    16), for a column of any type, such as ``text`` or ``uuid``. It counts
    the rows with one query and takes no ``strategy``. No index can serve
    hash predicates, so each partition's query reads the whole table.
    """
    if method not in METHODS:
        msg = f"method must be one of {', '.join(METHODS)}, not {method!r}"
        raise ValueError(msg)
    if method == "hash":
        if (strategy, sample_rows, max_sample_rows, seed) != (None, None, None, None):
            msg = "strategy, sample_rows, max_sample_rows and seed are for method='range'"
            raise ValueError(msg)
        return plan_from_hash(
            database, table, column, partitions, statement_timeout_ms=statement_timeout_ms
        )
    chosen = _strategy(
        "statistics" if strategy is None else strategy, sample_rows, max_sample_rows, seed
    )
    if chosen == "exact":
        return plan_from_exact(
            database, table, column, partitions, statement_timeout_ms=statement_timeout_ms
        )
    if chosen == "sample":
        return plan_from_sample(
            database,
            table,
            column,
            partitions,
            sample_rows=DEFAULT_SAMPLE_ROWS if sample_rows is None else sample_rows,
            max_sample_rows=max_sample_rows,
            seed=seed,
            statement_timeout_ms=statement_timeout_ms,
        )
    return plan_from_statistics(
        database, table, column, partitions, statement_timeout_ms=statement_timeout_ms
    )


def _strategy(
    strategy: str, sample_rows: int | None, max_sample_rows: int | None, seed: int | None
) -> Strategy:
    """Return the strategy, after checking that it is known and the options fit it."""
    if strategy not in STRATEGIES:
        msg = f"strategy must be one of {', '.join(STRATEGIES)}, not {strategy!r}"
        raise ValueError(msg)
    if strategy != "sample" and (sample_rows, max_sample_rows, seed) != (None, None, None):
        msg = "sample_rows, max_sample_rows and seed are for strategy='sample'"
        raise ValueError(msg)
    return strategy
