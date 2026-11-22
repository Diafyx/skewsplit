"""Plan from the statistics PostgreSQL keeps in ``pg_stats`` (spec section 13).

Nothing reads the table itself: the plan comes from what the last ``ANALYZE``
gathered, so planning takes the same few queries on a table of any size.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

from psycopg.rows import tuple_row

from skewsplit.core.pgstats import from_pg_stats
from skewsplit.core.planner import plan
from skewsplit.postgres.catalog import CatalogError, CatalogErrorCode, Column, find_column
from skewsplit.postgres.connection import check_timeout, connection, set_timeout

if TYPE_CHECKING:
    import psycopg

    from skewsplit.core.plan import JSON, Plan

# The text forms of spec section 13, for the reading transaction only.
_SETTINGS: Final = """
SELECT pg_catalog.set_config('DateStyle', 'ISO', true),
       pg_catalog.set_config('TimeZone', 'UTC', true),
       pg_catalog.set_config('extra_float_digits', '1', true)
"""

# The table and, recursively, its partitions and inheritance children. UNION
# drops the repeats that multiple inheritance can give.
_TREE: Final = """
WITH RECURSIVE tree(relid) AS (
  SELECT %(relid)s::oid
  UNION
  SELECT i.inhrelid FROM pg_catalog.pg_inherits i JOIN tree t ON i.inhparent = t.relid
)
SELECT c.relkind::text, c.reltuples::text,
       quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS name
FROM tree t
JOIN pg_catalog.pg_class c ON c.oid = t.relid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
ORDER BY name
"""

_FACTS: Final = """
SELECT GREATEST(s.last_analyze, s.last_autoanalyze)::text,
       floor(extract(epoch FROM now() - GREATEST(s.last_analyze, s.last_autoanalyze)))::bigint,
       s.n_mod_since_analyze
FROM pg_catalog.pg_stat_all_tables s
WHERE s.relid = %(relid)s::oid
"""

_STATISTICS: Final = """
SELECT s.null_frac::text, s.most_common_vals::text, s.most_common_freqs::text,
       s.histogram_bounds::text, s.n_distinct
FROM pg_catalog.pg_stats s
WHERE s.schemaname = %(schema)s AND s.tablename = %(table)s AND s.attname = %(column)s
  AND s.inherited = %(inherited)s
"""

# A partitioned table stores no rows; since PostgreSQL 14 ANALYZE gives it
# the total of its partitions, which would count them twice.
_PARTITIONED: Final = "p"


@dataclass(frozen=True)
class Statistics:
    """A column's statistics, as the texts spec section 13 converts.

    Attributes:
        column: The column they describe.
        inherited: Whether they cover the table with its partitions or
            inheritance children.
        reltuples: ``pg_class.reltuples`` of each relation they cover that
            stores rows.
        null_frac: ``pg_stats.null_frac``.
        most_common_vals: ``pg_stats.most_common_vals`` as array text, or ``None``.
        most_common_freqs: ``pg_stats.most_common_freqs`` as array text, or ``None``.
        histogram_bounds: ``pg_stats.histogram_bounds`` as array text, or ``None``.
        n_distinct: ``pg_stats.n_distinct``, or ``None`` for an empty table.
        analyzed_at: When the table was last analyzed, in the text form of
            ``timestamptz``, or ``None`` if PostgreSQL does not know.
        age_seconds: Whole seconds since then, by the server's clock, or ``None``.
        modified_since_analyze: Rows changed since then, or ``None`` if
            PostgreSQL does not count them.
    """

    column: Column
    inherited: bool
    reltuples: tuple[str, ...]
    null_frac: str
    most_common_vals: str | None
    most_common_freqs: str | None
    histogram_bounds: str | None
    n_distinct: float | None
    analyzed_at: str | None
    age_seconds: int | None
    modified_since_analyze: int | None

    def conversion_input(self, partitions: int) -> dict[str, object]:
        """Return the conversion input of spec section 13."""
        data: dict[str, object] = {"column": self.column.name, "type": self.column.type_name}
        if self.column.scale is not None:
            data["scale"] = self.column.scale
        data.update(
            partitions=partitions,
            reltuples=list(self.reltuples),
            null_frac=self.null_frac,
            most_common_vals=self.most_common_vals,
            most_common_freqs=self.most_common_freqs,
            histogram_bounds=self.histogram_bounds,
            modified_since_analyze=self.modified_since_analyze,
        )
        return data

    def report(self) -> dict[str, object]:
        """Return the fields this adapter adds to the plan's ``source``."""
        return {
            "table": self.column.qualified,
            "inherited": self.inherited,
            "analyzed_at": self.analyzed_at,
            "statistics_age_seconds": self.age_seconds,
            "n_distinct": self.n_distinct,
        }


def distribution(
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    *,
    statement_timeout_ms: int | None = None,
) -> JSON:
    """Return a column's distribution, from its ``pg_stats`` statistics.

    It is the input of spec section 1 without ``partitions``, which is what
    :func:`skewsplit.plan` takes, and its ``source`` says where it came from
    (spec section 13).

    Args:
        database: A libpq connection string, or an open connection, which is
            left open and with its settings and transaction as they were.
        table: The table as SQL names it, as for :func:`find_column`.
        column: The column's exact name, not quoted.
        statement_timeout_ms: A ``statement_timeout`` for the queries that
            read the statistics, in milliseconds; by default the session's.

    Raises:
        CatalogError: If the column cannot be split or has no statistics.
        SpecError: If the statistics do not convert into a valid input.
        ValueError: If ``statement_timeout_ms`` is not positive.
    """
    check_timeout(statement_timeout_ms)
    with connection(database) as conn:
        return _distribution(conn, table, column, statement_timeout_ms)


def plan_from_statistics(
    database: str | psycopg.Connection[Any],
    table: str,
    column: str,
    partitions: int,
    *,
    statement_timeout_ms: int | None = None,
) -> Plan:
    """Plan ``partitions`` predicates for a column from its ``pg_stats`` statistics.

    Args:
        database: A libpq connection string, or an open connection, which is
            left open and with its settings and transaction as they were.
        table: The table as SQL names it, as for :func:`find_column`.
        column: The column's exact name, not quoted.
        partitions: The number of predicates to plan besides the NULL predicate.
        statement_timeout_ms: As for :func:`distribution`.

    Raises:
        CatalogError: If the column cannot be split or has no statistics.
        SpecError: If the statistics do not convert into a valid input.
        ValueError: If ``statement_timeout_ms`` is not positive.
    """
    check_timeout(statement_timeout_ms)
    with connection(database) as conn:
        start = perf_counter()
        data = _distribution(conn, table, column, statement_timeout_ms)
    result = plan({**data, "partitions": partitions})
    elapsed_ms = (perf_counter() - start) * 1000
    source = cast("JSON", result.source)
    return replace(result, source={**source, "planning_ms": round(elapsed_ms, 3)})


def read_statistics(
    conn: psycopg.Connection[Any], column: Column, *, statement_timeout_ms: int | None = None
) -> Statistics:
    """Read a column's statistics and the row counts they go with.

    The table's own statistics are used, or the inherited ones if it has
    partitions or inheritance children. The reading transaction is rolled
    back, so the caller's settings and transaction are left as they were.
    ``statement_timeout_ms`` bounds each of its queries.

    Raises:
        CatalogError: ``statistics_missing`` if ``ANALYZE`` has to run first,
            or ``table_not_found`` if the table was dropped.
    """
    with conn.transaction(force_rollback=True), conn.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(_SETTINGS)
        set_timeout(cursor, statement_timeout_ms)
        tree = cursor.execute(_TREE, {"relid": column.relid}).fetchall()
        facts = cursor.execute(_FACTS, {"relid": column.relid}).fetchone()
        inherited = len(tree) > 1
        params = {
            "schema": column.schema,
            "table": column.table,
            "column": column.name,
            "inherited": inherited,
        }
        row = cursor.execute(_STATISTICS, params).fetchone()
    if not tree:
        _fail("table_not_found", f"{column.qualified} no longer exists")
    stored = [(name, rows) for kind, rows, name in tree if kind != _PARTITIONED]
    # Since PostgreSQL 14, -1 means never vacuumed or analyzed.
    unknown = [name for name, rows in stored if rows.startswith("-")]
    if unknown:
        has, them = ("has", "it") if len(unknown) == 1 else ("have", "them")
        _fail(
            "statistics_missing",
            f"{', '.join(unknown)} {has} never been vacuumed or analyzed, so PostgreSQL"
            f" has no row count for {them}; run ANALYZE on {them}",
        )
    if row is None:
        if any(rows != "0" for _, rows in stored):
            _fail("statistics_missing", _no_statistics(column, inherited=inherited))
        # ANALYZE keeps no statistics for an empty table.
        row = ("0", None, None, None, None)
    null_frac, values, freqs, bounds, n_distinct = row
    analyzed_at, age_seconds, modified = facts or (None, None, None)
    return Statistics(
        column=column,
        inherited=inherited,
        reltuples=tuple(rows for _, rows in stored) or ("0",),
        null_frac=null_frac,
        most_common_vals=values,
        most_common_freqs=freqs,
        histogram_bounds=bounds,
        n_distinct=n_distinct,
        analyzed_at=analyzed_at,
        age_seconds=age_seconds,
        # PostgreSQL does not count changes since a tree was analyzed as a whole.
        modified_since_analyze=None if inherited else modified,
    )


def _distribution(
    conn: psycopg.Connection[Any], table: str, column: str, statement_timeout_ms: int | None
) -> JSON:
    found = find_column(conn, table, column)
    statistics = read_statistics(conn, found, statement_timeout_ms=statement_timeout_ms)
    # The conversion does not depend on the partitions; any valid number will do.
    data = from_pg_stats(statistics.conversion_input(1))
    del data["partitions"]
    data["source"] = {**cast("JSON", data["source"]), **statistics.report()}
    return data


def _no_statistics(column: Column, *, inherited: bool) -> str:
    message = (
        f"pg_stats has no statistics for column {column.name!r} of {column.qualified};"
        f" run ANALYZE {column.qualified}"
    )
    if inherited:
        message += (
            ". The statistics of a table with partitions or inheritance children cover"
            " them all, and autovacuum never gathers them: it does not analyze"
            " partitioned tables, and changes to children do not make it analyze the parent"
        )
    return message


def _fail(code: CatalogErrorCode, message: str) -> NoReturn:
    raise CatalogError(code, message)
