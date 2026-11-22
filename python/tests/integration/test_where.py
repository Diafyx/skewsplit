"""Filtered reads (spec section 18) against PostgreSQL: plans for the rows a filter keeps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql

from skewsplit.postgres.connection import run
from skewsplit.postgres.planning import distribution, plan
from skewsplit.postgres.verify import verify

if TYPE_CHECKING:
    from collections.abc import Iterator

    from integration.conftest import Database
    from skewsplit.core.plan import Plan

# 40000 rows over 400 days. Before day 300, v is spread evenly; from day 300,
# the filtered part, a quarter of its rows are at 7. Notes start with 'a' on
# odd days, so a LIKE filter has a % in it.
TABLE = "where_orders"
SETUP = (
    "CREATE TABLE {t} (v integer, created_at date, note text) WITH (autovacuum_enabled = false)",
    (
        "INSERT INTO {t} SELECT CASE WHEN g % 400 >= 300 AND g % 4 = 0 THEN 7"
        " WHEN g % 50 = 0 THEN NULL ELSE g END,"
        " date '2025-01-01' + g % 400,"
        " CASE WHEN g % 400 % 2 = 1 THEN 'a' || g ELSE 'b' || g END"
        " FROM generate_series(1, 40000) AS g"
    ),
)
RECENT = "created_at >= date '2025-01-01' + 300 -- the last 100 days"
# The rows the filter keeps: days 300 to 399 of each 400.
RECENT_ROWS = 10000


@pytest.fixture
def conn(database: Database) -> Iterator[psycopg.Connection[Any]]:
    """A superuser connection whose search_path is the test schema."""
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
        yield connection


@pytest.fixture(scope="module")
def orders(database: Database) -> str:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        name = sql.Identifier(database.schema, TABLE)
        for statement in SETUP:
            admin.execute(sql.SQL(statement).format(t=name))
    return TABLE


def count(conn: psycopg.Connection[Any], where: str) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE {}").format(
        sql.Identifier(TABLE), sql.SQL(where)
    )
    row = conn.execute(query).fetchone()
    assert row is not None
    return int(row[0])


def check(conn: psycopg.Connection[Any], result: Plan, where: str = RECENT) -> tuple[int, ...]:
    """Every row the filter keeps matches exactly one predicate; return each one's rows."""
    found = verify(conn, TABLE, "v", result.predicates, counts=True, where=where)
    assert found.ok
    assert found.total_rows == count(conn, where)
    assert found.predicate_rows is not None
    assert sum(found.predicate_rows) == found.total_rows
    return found.predicate_rows


@pytest.mark.usefixtures("orders")
def test_a_sample_of_the_filtered_rows(conn: psycopg.Connection[Any]) -> None:
    result = plan(conn, TABLE, "v", 8, strategy="sample", where=RECENT)
    source: Any = result.source
    assert source["where"] == RECENT
    # Fewer rows than the sample size, so every filtered row was read.
    assert (source["method"], source["sample_rows"]) == ("all", RECENT_ROWS)
    assert result.total_rows == RECENT_ROWS
    actual = check(conn, result)
    # 7 holds a quarter of the filtered rows, so it gets its own predicate.
    assert '"v" = 7' in result.predicates
    assert actual[result.predicates.index('"v" = 7')] == 2500
    assert actual[0] == result.parts[0].estimated_rows


@pytest.mark.usefixtures("orders")
def test_exact_quantiles_of_the_filtered_rows(conn: psycopg.Connection[Any]) -> None:
    result = plan(conn, TABLE, "v", 8, strategy="exact", where=RECENT)
    assert result.source["where"] == RECENT  # type: ignore[index]
    assert result.total_rows == RECENT_ROWS
    actual = check(conn, result)
    for part, rows in zip(result.parts, actual):
        if part.kind != "range":
            assert rows == part.estimated_rows, part.sql


@pytest.mark.usefixtures("orders")
def test_hash_predicates_count_the_filtered_rows(conn: psycopg.Connection[Any]) -> None:
    result = plan(conn, TABLE, "v", 4, method="hash", where=RECENT)
    assert (result.total_rows, result.source["where"]) == (RECENT_ROWS, RECENT)  # type: ignore[index]
    check(conn, result)


@pytest.mark.usefixtures("orders")
def test_the_statistics_warn_that_they_ignore_the_filter(conn: psycopg.Connection[Any]) -> None:
    conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(TABLE)))
    result = plan(conn, TABLE, "v", 8, where=RECENT)
    source: Any = result.source
    assert [w["code"] for w in source["warnings"]][-1] == "filter_ignored"
    assert source["warnings"][-1]["where"] == RECENT
    # The estimates are of the whole table.
    assert result.total_rows == 40000
    assert "warning filter_ignored: pg_stats describes the whole table" in result.report
    # The predicates still cover every row the filter keeps.
    check(conn, result)


@pytest.mark.usefixtures("orders")
def test_auto_reads_a_sample_of_a_filtered_read(conn: psycopg.Connection[Any]) -> None:
    conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(TABLE)))
    result = plan(conn, TABLE, "v", 8, strategy="auto", where=RECENT)
    source: Any = result.source
    assert source["auto"]["reason"] == "filter_given"
    assert (source["statistics"], result.total_rows) == ("sample", RECENT_ROWS)
    data: Any = distribution(conn, TABLE, "v", strategy="auto", where=RECENT)
    assert data["source"]["auto"]["reason"] == "filter_given"


@pytest.mark.usefixtures("orders")
def test_a_filter_with_a_percent_sign(conn: psycopg.Connection[Any]) -> None:
    odd = "note LIKE 'a%'"
    result = plan(conn, TABLE, "v", 4, strategy="exact", where=odd)
    assert result.total_rows == count(conn, odd) == 20000
    check(conn, result, odd)


@pytest.mark.usefixtures("orders")
def test_verify_counts_only_the_filtered_rows(conn: psycopg.Connection[Any]) -> None:
    found = verify(conn, TABLE, "v", ['"v" IS NULL', '"v" IS NOT NULL'], counts=True, where=RECENT)
    nulls = count(conn, f"({RECENT}\n) AND v IS NULL")
    assert (found.total_rows, found.predicate_rows) == (RECENT_ROWS, (nulls, RECENT_ROWS - nulls))


def no_table(conn: psycopg.Connection[Any], name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NULL", (name,)).fetchone()
    return bool(row and row[0])


@pytest.mark.usefixtures("orders")
@pytest.mark.parametrize("strategy", ["sample", "exact"])
def test_a_filter_cannot_run_a_second_statement(
    conn: psycopg.Connection[Any], strategy: str
) -> None:
    attack = "true; COMMIT; CREATE TABLE where_pwned_filter (x integer); SELECT true"
    with pytest.raises(psycopg.errors.SyntaxError):
        plan(conn, TABLE, "v", 4, strategy=strategy, where=attack)  # type: ignore[arg-type]
    assert no_table(conn, "where_pwned_filter")


@pytest.mark.usefixtures("orders")
def test_a_predicate_cannot_run_a_second_statement(conn: psycopg.Connection[Any]) -> None:
    attack = "true); COMMIT; CREATE TABLE where_pwned_predicate (x integer); SELECT (true"
    with pytest.raises(psycopg.errors.SyntaxError):
        verify(conn, TABLE, "v", [attack])
    assert no_table(conn, "where_pwned_predicate")


@pytest.mark.usefixtures("orders")
def test_a_filter_cannot_write(conn: psycopg.Connection[Any]) -> None:
    # One statement, but it tries to write: the read-only transaction refuses.
    attack = "nextval('where_sequence') > 0"
    conn.execute("CREATE SEQUENCE where_sequence")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        plan(conn, TABLE, "v", 4, strategy="exact", where=attack)
    row = conn.execute("SELECT last_value, is_called FROM where_sequence").fetchone()
    assert row == (1, False)


def test_run_refuses_a_second_valid_statement(conn: psycopg.Connection[Any]) -> None:
    # Both statements are valid SQL: only the extended protocol stops the second.
    query = sql.SQL("SELECT 1; CREATE TABLE where_pwned_run (x integer)")
    with (
        conn.cursor() as cursor,
        pytest.raises(psycopg.errors.SyntaxError, match="multiple commands"),
    ):
        run(cursor, query)
    assert no_table(conn, "where_pwned_run")
    # The simple protocol, which run never uses, would have run both.
    with conn.transaction(force_rollback=True):
        conn.execute(query)
        assert not no_table(conn, "where_pwned_run")
    assert no_table(conn, "where_pwned_run")
