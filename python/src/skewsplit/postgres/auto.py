"""Choose the cheapest strategy that gives a plan to trust (spec section 17).

The statistics cost a few catalog queries, so they come first when they
exist and are fresh. Otherwise a sample of the table is read. A column that
ranges cannot split gets hash predicates. Reading every row (``exact``) is
never chosen: only the caller knows when it is worth its cost.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, cast

from skewsplit.core.planner import plan
from skewsplit.postgres.catalog import CatalogError, find_column
from skewsplit.postgres.connection import check_timeout, connection
from skewsplit.postgres.hashing import plan_from_hash
from skewsplit.postgres.sample import DEFAULT_SAMPLE_ROWS, plan_from_sample, sample_distribution
from skewsplit.postgres.statistics import distribution_from, read_statistics

if TYPE_CHECKING:
    import psycopg

    from skewsplit.core.plan import JSON, Plan
    from skewsplit.postgres.catalog import Column

Chosen = Literal["statistics", "sample", "hash"]
Reason = Literal[
    "unsupported_type",
    "filter_given",
    "statistics_missing",
    "statistics_stale",
    "statistics_unchecked",
    "statistics_fresh",
]


@dataclass(frozen=True)
class Choice:
    """The strategy chosen for a column, and why.

    Attributes:
        strategy: ``statistics``, ``sample`` or ``hash``.
        reason: The row of spec section 17's table that chose it.
        message: The reason for people.
        distribution: For ``statistics``, the distribution already read, so
            planning reads it only once; otherwise ``None``.
    """

    strategy: Chosen
    reason: Reason
    message: str
    distribution: JSON | None = None

    def report(self) -> dict[str, object]:
        """Return the ``auto`` field of the plan's ``source``."""
        return {"strategy": self.strategy, "reason": self.reason, "message": self.message}


def choose(
    conn: psycopg.Connection[Any],
    column: Column,
    *,
    statement_timeout_ms: int | None = None,
    where: str | None = None,
) -> Choice:
    """Choose the strategy for a column, by the table of spec section 17.

    With ``where``, the read is filtered, and ``pg_stats`` describes the whole
    table, so a sample of the rows the filter keeps is read instead.

    Raises:
        CatalogError: If the statistics cannot be read for another reason
            than that they are missing, such as a dropped table.
        SpecError: If the statistics do not convert into a valid input.
    """
    if not column.splittable:
        return Choice(
            "hash",
            "unsupported_type",
            f"ranges cannot split {column.type_name}, so hash predicates split it;"
            " each partition reads the whole table",
        )
    if where is not None:
        return Choice(
            "sample",
            "filter_given",
            "the read is filtered, and pg_stats describes the whole table, so a sample"
            " of the rows the filter keeps is read",
        )
    try:
        statistics = read_statistics(conn, column, statement_timeout_ms=statement_timeout_ms)
    except CatalogError as error:
        if error.code != "statistics_missing":
            raise
        return Choice("sample", "statistics_missing", f"no statistics to use: {error.message}")
    data = distribution_from(statistics)
    source = cast("JSON", data["source"])
    stale = [
        warning
        for warning in cast("list[JSON]", source["warnings"])
        if warning["code"] == "stale_statistics"
    ]
    if stale:
        return Choice(
            "sample",
            "statistics_stale",
            f"the statistics are out of date: {stale[0]['modified_rows']} of about"
            f" {stale[0]['rows']} rows changed since the last ANALYZE",
        )
    if statistics.modified_since_analyze is None:
        return Choice(
            "sample",
            "statistics_unchecked",
            "PostgreSQL does not count the rows changed since the statistics were"
            " gathered, so they may be out of date",
        )
    return Choice(
        "statistics",
        "statistics_fresh",
        f"the statistics are fresh: {statistics.modified_since_analyze} rows changed"
        " since the last ANALYZE, a tenth or less",
        data,
    )


def auto_distribution(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    *,
    sample_rows: int | None = None,
    max_sample_rows: int | None = None,
    seed: int | None = None,
    statement_timeout_ms: int | None = None,
    where: str | None = None,
) -> JSON:
    """Return a column's distribution from the statistics or a sample, as spec section 17 chooses.

    A distribution is for ranges, so a column they cannot split is an error,
    as with the other strategies. The sample options apply when a sample is
    read. ``source`` has the ``auto`` field of :meth:`Choice.report`.

    Raises:
        CatalogError: If the column cannot be split by ranges.
        SpecError: If what was read does not convert into a valid input.
        ValueError: If an option is out of range.
        psycopg.Error: If a query fails or was cancelled by the timeout.
    """
    check_timeout(statement_timeout_ms)
    with connection(database) as conn:
        found = find_column(conn, table, column)
        choice = choose(conn, found, statement_timeout_ms=statement_timeout_ms, where=where)
        data = choice.distribution
        if data is None:
            data = sample_distribution(
                conn,
                table,
                column,
                sample_rows=DEFAULT_SAMPLE_ROWS if sample_rows is None else sample_rows,
                max_sample_rows=max_sample_rows,
                seed=seed,
                statement_timeout_ms=statement_timeout_ms,
                where=where,
            )
    return {**data, "source": {**cast("JSON", data["source"]), "auto": choice.report()}}


def plan_auto(  # noqa: PLR0913 -- the keyword-only options are the CLI's flags
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    partitions: int,
    *,
    hash_allowed: bool = True,
    sample_rows: int | None = None,
    max_sample_rows: int | None = None,
    seed: int | None = None,
    statement_timeout_ms: int | None = None,
    where: str | None = None,
) -> Plan:
    """Plan ``partitions`` predicates with the strategy that spec section 17 chooses.

    Args:
        database: A libpq connection string, or an open connection, which is
            left open and with its settings and transaction as they were.
        table: The table as SQL names it, as for :func:`find_column`.
        column: The column's exact name, not quoted.
        partitions: The number of predicates besides the NULL predicate.
        hash_allowed: Whether a column that ranges cannot split gets hash
            predicates; without it, such a column is an error.
        sample_rows: As for the sample strategy, when a sample is read.
        max_sample_rows: As for the sample strategy, when a sample is read.
        seed: As for the sample strategy, when a sample is read.
        statement_timeout_ms: A ``statement_timeout`` for each query.
        where: A filter for the read (spec section 18): a sample or a count
            of the rows it keeps, never the statistics.

    The plan's ``source`` has the ``auto`` field of :meth:`Choice.report`,
    and ``planning_ms`` includes the time spent choosing.

    Raises:
        CatalogError: If the column cannot be planned.
        SpecError: If what was read does not convert into a valid input.
        ValueError: If an option is out of range.
        psycopg.Error: If a query fails or was cancelled by the timeout.
    """
    check_timeout(statement_timeout_ms)
    with connection(database) as conn:
        start = perf_counter()
        found = find_column(conn, table, column, any_type=hash_allowed)
        choice = choose(conn, found, statement_timeout_ms=statement_timeout_ms, where=where)
        if choice.strategy == "hash":
            result = plan_from_hash(
                conn,
                table,
                column,
                partitions,
                statement_timeout_ms=statement_timeout_ms,
                where=where,
            )
        elif choice.distribution is None:
            result = plan_from_sample(
                conn,
                table,
                column,
                partitions,
                sample_rows=DEFAULT_SAMPLE_ROWS if sample_rows is None else sample_rows,
                max_sample_rows=max_sample_rows,
                seed=seed,
                statement_timeout_ms=statement_timeout_ms,
                where=where,
            )
        else:
            result = plan({**choice.distribution, "partitions": partitions})
    elapsed_ms = (perf_counter() - start) * 1000
    source = {**cast("JSON", result.source), "auto": choice.report()}
    return replace(result, source={**source, "planning_ms": round(elapsed_ms, 3)})
