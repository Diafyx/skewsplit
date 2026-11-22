"""Plan from a table's statistics or from a sample of it: the strategy is the caller's choice.

``statistics`` reads ``pg_stats`` and never the table (spec section 13).
``sample`` reads a sample of the table with ``TABLESAMPLE`` (section 14), for
when the statistics are missing or out of date.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Literal

from skewsplit.postgres.sample import DEFAULT_SAMPLE_ROWS, plan_from_sample, sample_distribution
from skewsplit.postgres.statistics import distribution as statistics_distribution
from skewsplit.postgres.statistics import plan_from_statistics

if TYPE_CHECKING:
    import psycopg

    from skewsplit.core.plan import JSON, Plan

Strategy = Literal["statistics", "sample"]
STRATEGIES: Final[tuple[Strategy, ...]] = ("statistics", "sample")


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
        strategy: ``statistics`` to read ``pg_stats``, or ``sample`` to read
            a sample of the table.
        sample_rows: For ``sample``: about how many rows to sample, 30000 by
            default.
        max_sample_rows: For ``sample``: the most rows the sample may read,
            10 times ``sample_rows`` by default.
        seed: For ``sample``: a ``REPEATABLE`` seed, so that the same table
            gives the same sample.
        statement_timeout_ms: A ``statement_timeout`` for each query that
            reads the statistics or the sample, in milliseconds.

    Raises:
        CatalogError: If the column cannot be split, or has no statistics
            for ``statistics``.
        SpecError: If what was read does not convert into a valid input.
        ValueError: If the strategy is unknown, or an option is out of range
            or given for the other strategy.
        psycopg.Error: If a query fails or was cancelled by the timeout.
    """
    if _sampling(strategy, sample_rows, max_sample_rows, seed):
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
    strategy: Strategy = "statistics",
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
    """
    if _sampling(strategy, sample_rows, max_sample_rows, seed):
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


def _sampling(
    strategy: str, sample_rows: int | None, max_sample_rows: int | None, seed: int | None
) -> bool:
    """Whether the strategy is ``sample``, after checking the options fit it."""
    if strategy not in STRATEGIES:
        msg = f"strategy must be one of {', '.join(STRATEGIES)}, not {strategy!r}"
        raise ValueError(msg)
    if strategy == "statistics" and (sample_rows, max_sample_rows, seed) != (None, None, None):
        msg = "sample_rows, max_sample_rows and seed are for strategy='sample'"
        raise ValueError(msg)
    return strategy == "sample"
