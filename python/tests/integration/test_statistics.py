"""Plans from pg_stats against real statistics, checked against the rows themselves.

Every table here has at most 30000 rows, so ANALYZE (at the default
statistics target of 100) reads every row and the statistics are the same
on every run. That is the case spec section 13 bounds the error for.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

from skewsplit.core.pgstats import from_pg_stats
from skewsplit.postgres.catalog import CatalogError, find_column
from skewsplit.postgres.statistics import plan_from_statistics, read_statistics

if TYPE_CHECKING:
    from collections.abc import Iterator

    from integration.conftest import Database
    from skewsplit.core.plan import Plan

# Each table has one column, v, filled by its query.
DISTRIBUTIONS = {
    "uniform": ("integer", "SELECT g FROM generate_series(1, 30000) AS g"),
    "dominant": (
        "bigint",
        "SELECT CASE WHEN g % 5 < 2 THEN 42 ELSE g * 1000 END FROM generate_series(1, 30000) AS g",
    ),
    "null_heavy": (
        "integer",
        "SELECT CASE WHEN g % 10 < 6 THEN NULL ELSE g END FROM generate_series(1, 30000) AS g",
    ),
    "gaps": (
        "integer",
        (
            "SELECT CASE WHEN g <= 10000 THEN g WHEN g <= 20000 THEN 1000000 + g"
            " ELSE 50000000 + g END FROM generate_series(1, 30000) AS g"
        ),
    ),
    "small": ("smallint", "SELECT g % 20000 - 10000 FROM generate_series(1, 30000) AS g"),
    "amounts": ("numeric(12, 2)", "SELECT g * g / 100.0 FROM generate_series(1, 30000) AS g"),
    "dates": (
        "date",
        "SELECT date '2000-01-01' + g * g / 100000 FROM generate_series(0, 29999) AS g",
    ),
    "timestamps": (
        "timestamp",
        (
            "SELECT timestamp '2026-01-01' + g * g * interval '1 millisecond'"
            " FROM generate_series(1, 30000) AS g"
        ),
    ),
    "timestamptzs": (
        "timestamptz",
        (
            "SELECT timestamptz '2026-01-01 00:00:00+00' + g / 7 * interval '1 hour'"
            " + g % 7 * interval '1 microsecond' FROM generate_series(1, 30000) AS g"
        ),
    ),
}


# server_version of PostgreSQL 18.0, which changed ANALYZE on inheritance parents.
POSTGRES_18 = 180000


def flush_statistics(conn: psycopg.Connection[Any]) -> None:
    # This session's row counts reach the statistics system when it next goes
    # idle; this makes that happen after this statement (PostgreSQL 15 and later).
    conn.execute("SELECT pg_stat_force_next_flush()")


def run(conn: psycopg.Connection[Any], template: str, **tables: str) -> None:
    """Run a statement with the test schema's tables filled in by name."""
    schema = conn.execute("SELECT current_schema()").fetchone()
    assert schema is not None
    names = {key: sql.Identifier(schema[0], name) for key, name in tables.items()}
    conn.execute(sql.SQL(template).format(**names))


@pytest.fixture
def conn(database: Database) -> Iterator[psycopg.Connection[Any]]:
    """A superuser connection whose search_path is the test schema."""
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
        connection.execute("SET default_statistics_target = 100")
        yield connection


@pytest.fixture(scope="module")
def distributions(database: Database) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
        admin.execute("SET default_statistics_target = 100")
        for name, (sql_type, query) in DISTRIBUTIONS.items():
            run(
                admin,
                "CREATE TABLE {t} (v " + sql_type + ") WITH (autovacuum_enabled = false)",
                t=name,
            )
            run(admin, "INSERT INTO {t} (v) " + query, t=name)
        flush_statistics(admin)
        for name in DISTRIBUTIONS:
            run(admin, "ANALYZE {t}", t=name)


def count(conn: psycopg.Connection[Any], table: str, where: str) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE {}")
    row = conn.execute(query.format(sql.Identifier(table), sql.SQL(where))).fetchone()
    assert row is not None
    return int(row[0])


def check_accuracy(conn: psycopg.Connection[Any], table: str, plan: Plan) -> None:
    """Check the bounds of spec section 13 on the rows each predicate actually reads."""
    partitions = plan.requested_partitions
    stats = read_statistics(conn, find_column(conn, table, "v"))
    histogram: Any = from_pg_stats(stats.conversion_input(partitions))["histogram"]
    # B: the most rows of any bucket, ceil(H / m).
    bucket = 0 if histogram is None else -(-histogram["rows"] // (len(histogram["bounds"]) - 1))
    actual = [count(conn, table, predicate.sql) for predicate in plan.predicates]
    total = count(conn, table, "true")
    # Every row exactly once, and the row counts are exact.
    assert sum(actual) == total == plan.total_rows
    for predicate, rows in zip(plan.predicates, actual):
        if predicate.kind == "range":
            assert abs(rows - predicate.estimated_rows) <= 2 * bucket, predicate.sql
        else:
            assert rows == predicate.estimated_rows, predicate.sql
    # Each skew score is rounded to 3 decimals, so they can differ by 0.001 more.
    assert plan.skew_milli is not None
    actual_milli = (2 * max(actual) * partitions * 1000 + total) // (2 * total)
    assert abs(actual_milli - plan.skew_milli) * total <= 2000 * bucket * partitions + total


@pytest.mark.usefixtures("distributions")
@pytest.mark.parametrize("partitions", [4, 16])
@pytest.mark.parametrize("name", list(DISTRIBUTIONS))
def test_estimates_agree_with_the_rows(
    conn: psycopg.Connection[Any], name: str, partitions: int
) -> None:
    result = plan_from_statistics(conn, name, "v", partitions)
    assert result.source["unestimated_rows"] == 0  # type: ignore[index]
    check_accuracy(conn, name, result)


@pytest.mark.usefixtures("distributions")
def test_the_warnings_of_skewed_columns(conn: psycopg.Connection[Any]) -> None:
    dominant = plan_from_statistics(conn, "dominant", "v", 8)
    assert [(w.code, w.value, w.rows) for w in dominant.warnings] == [
        ("dominant_value", "42", 12000)
    ]
    assert dominant.predicates[1].sql == '"v" < 42'
    nulls = plan_from_statistics(conn, "null_heavy", "v", 4)
    assert [(w.code, w.rows) for w in nulls.warnings] == [("nulls_exceed_share", 18000)]


@pytest.mark.usefixtures("distributions")
def test_the_report(conn: psycopg.Connection[Any], database: Database) -> None:
    source: Any = plan_from_statistics(conn, "uniform", "v", 4).source
    assert source["statistics"] == "pg_stats"
    assert (source["rows_estimate"], source["modified_since_analyze"]) == (30000, 0)
    assert source["warnings"] == []
    assert source["table"] == f"{database.schema}.uniform"
    assert source["inherited"] is False
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d{1,6})?\+00", source["analyzed_at"])
    assert 0 <= source["statistics_age_seconds"] < 600
    # Every value is distinct.
    assert source["n_distinct"] == -1
    assert source["planning_ms"] >= 0


def test_special_values_are_left_out_but_still_read(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v date) WITH (autovacuum_enabled = false)", t="infinite")
    run(
        conn,
        "INSERT INTO {t} SELECT CASE WHEN g % 10 = 0 THEN 'infinity'::date"
        " WHEN g % 10 = 1 THEN '-infinity'::date ELSE date '2000-01-01' + g END"
        " FROM generate_series(1, 30000) AS g",
        t="infinite",
    )
    flush_statistics(conn)
    run(conn, "ANALYZE {t}", t="infinite")
    result = plan_from_statistics(conn, "infinite", "v", 4)
    source: Any = result.source
    assert source["unestimated_rows"] == 6000
    assert [w["code"] for w in source["warnings"]] == ["special_values_left_out"]
    assert result.total_rows == 24000
    actual = [count(conn, "infinite", predicate.sql) for predicate in result.predicates]
    assert sum(actual) == 30000


def create_partitioned(conn: psycopg.Connection[Any]) -> None:
    """10000 rows in three partitions, two of them under a partitioned partition."""
    run(conn, "CREATE TABLE {p} (v integer) PARTITION BY RANGE (v)", p="parted")
    run(
        conn,
        "CREATE TABLE {a} PARTITION OF {p} FOR VALUES FROM (MINVALUE) TO (5000)"
        " WITH (autovacuum_enabled = false)",
        p="parted",
        a="parted_a",
    )
    run(
        conn,
        "CREATE TABLE {b} PARTITION OF {p} FOR VALUES FROM (5000) TO (MAXVALUE)"
        " PARTITION BY RANGE (v)",
        p="parted",
        b="parted_b",
    )
    for name, bounds in (("parted_b1", "(5000) TO (7500)"), ("parted_b2", "(7500) TO (MAXVALUE)")):
        run(
            conn,
            "CREATE TABLE {c} PARTITION OF {b} FOR VALUES FROM "
            + bounds
            + " WITH (autovacuum_enabled = false)",
            b="parted_b",
            c=name,
        )
    run(conn, "INSERT INTO {p} SELECT g FROM generate_series(1, 10000) AS g", p="parted")


def code_and_message(conn: psycopg.Connection[Any], table: str) -> tuple[str, str]:
    with pytest.raises(CatalogError) as caught:
        plan_from_statistics(conn, table, "v", 4)
    return caught.value.code, caught.value.message


def test_a_partitioned_table(conn: psycopg.Connection[Any]) -> None:
    create_partitioned(conn)
    code, message = code_and_message(conn, "parted")
    assert code == "statistics_missing"
    assert "never been vacuumed or analyzed" in message

    # The partitions alone: no statistics for the table as a whole.
    for name in ("parted_a", "parted_b1", "parted_b2"):
        run(conn, "ANALYZE {t}", t=name)
    code, message = code_and_message(conn, "parted")
    assert code == "statistics_missing"
    assert "autovacuum never gathers them" in message

    # ANALYZE on the table gives its partitioned partition a row count too,
    # which must not count those rows twice.
    run(conn, "ANALYZE {t}", t="parted")
    result = plan_from_statistics(conn, "parted", "v", 4)
    source: Any = result.source
    assert source["inherited"] is True
    assert source["rows_estimate"] == 10000
    assert source["modified_since_analyze"] is None
    check_accuracy(conn, "parted", result)


def test_inheritance_children(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {p} (v integer) WITH (autovacuum_enabled = false)", p="parent")
    run(
        conn,
        "CREATE TABLE {c} () INHERITS ({p}) WITH (autovacuum_enabled = false)",
        p="parent",
        c="child",
    )
    run(conn, "INSERT INTO {p} SELECT g FROM generate_series(1, 4000) AS g", p="parent")
    run(conn, "INSERT INTO {c} SELECT g FROM generate_series(4001, 10000) AS g", c="child")
    # ANALYZE on the parent gathers the statistics of the whole tree. Before
    # PostgreSQL 18 it leaves the child's row count unknown; since 18 it
    # analyzes the child too, unless ONLY (new in 18) is given.
    recursive = conn.info.server_version >= POSTGRES_18
    run(conn, "ANALYZE ONLY {p}" if recursive else "ANALYZE {p}", p="parent")
    code, message = code_and_message(conn, "parent")
    assert code == "statistics_missing"
    assert message.endswith("has no row count for it; run ANALYZE on it")
    if recursive:
        run(conn, "ANALYZE {p}", p="parent")
    else:
        run(conn, "ANALYZE {c}", c="child")
    result = plan_from_statistics(conn, "parent", "v", 4)
    source: Any = result.source
    assert source["inherited"] is True
    assert source["rows_estimate"] == 10000
    check_accuracy(conn, "parent", result)


def test_a_table_that_was_never_analyzed(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v integer) WITH (autovacuum_enabled = false)", t="fresh")
    run(conn, "INSERT INTO {t} SELECT generate_series(1, 100)", t="fresh")
    code, message = code_and_message(conn, "fresh")
    assert code == "statistics_missing"
    assert message.endswith("run ANALYZE on it")


def test_a_column_added_after_analyze(conn: psycopg.Connection[Any], database: Database) -> None:
    run(conn, "CREATE TABLE {t} (id integer) WITH (autovacuum_enabled = false)", t="grown")
    run(conn, "INSERT INTO {t} SELECT generate_series(1, 100)", t="grown")
    run(conn, "ANALYZE {t}", t="grown")
    run(conn, "ALTER TABLE {t} ADD COLUMN v integer", t="grown")
    code, message = code_and_message(conn, "grown")
    assert code == "statistics_missing"
    assert message.endswith(f"run ANALYZE {database.schema}.grown")


def test_an_empty_table(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v timestamptz) WITH (autovacuum_enabled = false)", t="empty")
    run(conn, "ANALYZE {t}", t="empty")
    result = plan_from_statistics(conn, "empty", "v", 4)
    assert result.total_rows == 0
    assert [w.code for w in result.warnings] == ["fewer_partitions", "no_rows"]
    source: Any = result.source
    assert source["n_distinct"] is None


def test_stale_statistics(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v integer) WITH (autovacuum_enabled = false)", t="changing")
    run(conn, "INSERT INTO {t} SELECT generate_series(1, 1000)", t="changing")
    flush_statistics(conn)
    run(conn, "ANALYZE {t}", t="changing")

    def warnings() -> list[tuple[object, ...]]:
        flush_statistics(conn)
        source: Any = plan_from_statistics(conn, "changing", "v", 4).source
        return [(w["code"], w["modified_rows"], w["rows"]) for w in source["warnings"]]

    # A tenth of the rows is not more than a tenth.
    run(conn, "UPDATE {t} SET v = -v WHERE v <= 100", t="changing")
    assert warnings() == []
    run(conn, "UPDATE {t} SET v = -v WHERE v = 101", t="changing")
    assert warnings() == [("stale_statistics", 101, 1000)]


def test_a_role_with_only_column_privileges(database: Database) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as reader:
        reader.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(database.role)))
        result = plan_from_statistics(reader, f"{database.schema}.secret", "id", 4)
    assert result.total_rows == 100


@pytest.mark.usefixtures("distributions")
def test_the_callers_settings_and_transaction_are_kept(database: Database) -> None:
    expected = plan_from_statistics(database.dsn, f"{database.schema}.timestamptzs", "v", 8)
    with psycopg.connect(database.dsn) as caller:
        caller.execute("SET DateStyle = 'German'")
        caller.execute("SET TimeZone = 'Asia/Kolkata'")
        caller.execute("SET extra_float_digits = -3")
        assert caller.info.transaction_status == TransactionStatus.INTRANS
        result = plan_from_statistics(caller, f"{database.schema}.timestamptzs", "v", 8)
        assert result.predicates == expected.predicates
        assert caller.info.transaction_status == TransactionStatus.INTRANS
        settings = caller.execute(
            "SELECT current_setting('DateStyle'), current_setting('TimeZone'),"
            " current_setting('extra_float_digits')"
        ).fetchone()
        assert settings == ("German, DMY", "Asia/Kolkata", "-3")
