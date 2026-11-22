"""Hash plans (spec section 16) on real tables: verify finds every row exactly once.

The values are deterministic, so each server gives the same remainders on
every run, and hashtextextended does not change between PostgreSQL versions.
"""

from __future__ import annotations

from math import sqrt
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

from skewsplit.postgres.hashing import plan_from_hash
from skewsplit.postgres.planning import plan
from skewsplit.postgres.verify import verify

if TYPE_CHECKING:
    from collections.abc import Iterator

    from integration.conftest import Database
    from skewsplit.core.plan import Plan

# Each table has one column, v, filled by its query.
TABLES = {
    # 100000 distinct uuids, 5% NULL.
    "hash_uuid": (
        "uuid",
        (
            "SELECT CASE WHEN g % 20 = 0 THEN NULL ELSE md5(g::text)::uuid END"
            " FROM generate_series(1, 100000) AS g"
        ),
    ),
    "hash_text": (
        "text",
        (
            "SELECT CASE WHEN g % 10 = 0 THEN NULL ELSE 'customer-' || g END"
            " FROM generate_series(1, 100000) AS g"
        ),
    ),
    "hash_float": ("double precision", "SELECT g * 0.1 FROM generate_series(1, 50000) AS g"),
    "hash_jsonb": (
        "jsonb",
        "SELECT jsonb_build_object('id', g) FROM generate_series(1, 50000) AS g",
    ),
    # A type ranges could split; hash predicates work on it too.
    "hash_integer": ("integer", "SELECT g FROM generate_series(1, 50000) AS g"),
    # Two values: at most two remainders get rows.
    "hash_boolean": (
        "boolean",
        (
            "SELECT CASE WHEN g % 7 = 0 THEN NULL ELSE g % 3 = 0 END"
            " FROM generate_series(1, 3000) AS g"
        ),
    ),
    # 40% of the rows at one value, which no hash plan can split.
    "hash_dominant": (
        "text",
        "SELECT CASE WHEN g % 5 < 2 THEN 'x' ELSE 'v' || g END FROM generate_series(1, 20000) AS g",
    ),
}
# The tables with one row per value, where the hash spreads rows like values.
DISTINCT = ("hash_uuid", "hash_text", "hash_float", "hash_jsonb", "hash_integer")


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
        yield connection


@pytest.fixture(scope="module")
def tables(database: Database) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
        for name, (sql_type, query) in TABLES.items():
            run(
                admin,
                "CREATE TABLE {t} (v " + sql_type + ") WITH (autovacuum_enabled = false)",
                t=name,
            )
            run(admin, "INSERT INTO {t} (v) " + query, t=name)


def check_hash(conn: psycopg.Connection[Any], table: str, result: Plan) -> tuple[int, ...]:
    """Check that every row matches exactly one predicate; return each one's rows."""
    found = verify(conn, table, "v", result.predicates, counts=True)
    assert found.ok
    assert found.predicate_rows is not None
    assert found.total_rows == result.total_rows == sum(found.predicate_rows)
    assert found.predicate_rows[0] == result.parts[0].estimated_rows
    assert result.planned_partitions == result.requested_partitions
    assert result.warnings[0].code == "full_scans"
    return found.predicate_rows


@pytest.mark.usefixtures("tables")
@pytest.mark.parametrize("partitions", [1, 4, 16])
@pytest.mark.parametrize("name", list(TABLES))
def test_every_row_matches_exactly_one_hash_predicate(
    conn: psycopg.Connection[Any], name: str, partitions: int
) -> None:
    result = plan_from_hash(conn, name, "v", partitions)
    row = conn.execute(sql.SQL("SELECT count(*), count(v) FROM {}").format(sql.Identifier(name)))
    total, non_null = row.fetchone() or (0, 0)
    assert (result.total_rows, result.null_rows) == (total, total - non_null)
    check_hash(conn, name, result)


@pytest.mark.usefixtures("tables")
@pytest.mark.parametrize("partitions", [4, 16])
@pytest.mark.parametrize("name", DISTINCT)
def test_distinct_values_spread_evenly(
    conn: psycopg.Connection[Any], name: str, partitions: int
) -> None:
    result = plan_from_hash(conn, name, "v", partitions)
    actual = check_hash(conn, name, result)
    rows = result.total_rows - result.null_rows
    # A good hash puts each row in a remainder with probability 1 / N, so a
    # remainder's rows vary by sqrt(R x (1/N) x (1 - 1/N)); allow six times that.
    spread = 6 * sqrt(rows * (1 / partitions) * (1 - 1 / partitions)) + 1
    for part, count in zip(result.parts[1:], actual[1:]):
        assert abs(count - part.estimated_rows) <= spread, part.sql


@pytest.mark.usefixtures("tables")
def test_two_values_fill_at_most_two_remainders(conn: psycopg.Connection[Any]) -> None:
    result = plan_from_hash(conn, "hash_boolean", "v", 8)
    actual = check_hash(conn, "hash_boolean", result)
    assert result.type_name == "boolean"
    assert 1 <= sum(1 for count in actual[1:] if count) <= 2


@pytest.mark.usefixtures("tables")
def test_a_dominant_value_stays_in_one_remainder(conn: psycopg.Connection[Any]) -> None:
    result = plan_from_hash(conn, "hash_dominant", "v", 4)
    actual = check_hash(conn, "hash_dominant", result)
    # The estimates assume even rows per value; 'x' alone has 8000 of 20000.
    assert max(result.parts[1:], key=lambda part: part.estimated_rows).estimated_rows == 5000
    assert max(actual[1:]) >= 8000


def test_an_empty_table_has_no_rows(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v text)", t="hash_empty")
    result = plan_from_hash(conn, "hash_empty", "v", 3)
    assert (result.total_rows, result.skew_milli) == (0, None)
    assert [w.code for w in result.warnings] == ["full_scans", "no_rows"]
    assert verify(conn, "hash_empty", "v", result.predicates).ok


def test_the_text_of_a_timestamptz_follows_the_time_zone(conn: psycopg.Connection[Any]) -> None:
    # Spec section 16: the readers must share the settings the text depends on.
    run(
        conn,
        "CREATE TABLE {t} AS SELECT timestamptz '2026-01-01 00:00:00+00'"
        " + g * interval '1 minute' AS v FROM generate_series(1, 1000) AS g",
        t="hash_tz",
    )
    result = plan_from_hash(conn, "hash_tz", "v", 4)
    remainder = result.predicates[1].rsplit(" = ", 1)[0]
    query = sql.SQL("SELECT array_agg({} ORDER BY v) FROM hash_tz").format(sql.SQL(remainder))
    remainders = {}
    for zone in ("UTC", "Asia/Kolkata"):
        with conn.transaction():
            conn.execute(sql.SQL("SET LOCAL TimeZone = {}").format(sql.Literal(zone)))
            row = conn.execute(query).fetchone()
            assert row is not None
            remainders[zone] = row[0]
    moved = sum(a != b for a, b in zip(remainders["UTC"], remainders["Asia/Kolkata"]))
    assert moved > 0
    # In one session, the plan is still exact.
    assert verify(conn, "hash_tz", "v", result.predicates).ok


def test_the_callers_transaction_and_settings_are_left_as_they_were(
    conn: psycopg.Connection[Any],
) -> None:
    run(
        conn,
        "CREATE TABLE {t} AS SELECT g::text AS v FROM generate_series(1, 100) AS g",
        t="hash_tx",
    )
    with conn.transaction():
        conn.execute("SET LOCAL TimeZone = 'Asia/Kolkata'")
        result = plan_from_hash(conn, "hash_tx", "v", 4, statement_timeout_ms=60000)
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        settings = conn.execute(
            "SELECT current_setting('TimeZone'), current_setting('transaction_read_only'),"
            " current_setting('statement_timeout')"
        ).fetchone()
        assert settings == ("Asia/Kolkata", "off", "0")
    assert result.total_rows == 100


def test_the_method_plans_hash_predicates(database: Database) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        name = sql.Identifier(database.schema, "hash_api")
        admin.execute(sql.SQL("CREATE TABLE {} (v uuid)").format(name))
        admin.execute(
            sql.SQL(
                "INSERT INTO {} SELECT md5(g::text)::uuid FROM generate_series(1, 1000) AS g"
            ).format(name)
        )
    table = f"{database.schema}.hash_api"
    result = plan(database.dsn, table, "v", 4, method="hash")
    source: Any = result.source
    assert (source["statistics"], source["rows"]) == ("count", 1000)
    assert verify(database.dsn, table, "v", result.predicates).ok


def test_the_timeout_cancels_a_slow_count(database: Database) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        name = sql.Identifier(database.schema, "hash_large")
        admin.execute(
            sql.SQL(
                "CREATE UNLOGGED TABLE {} AS SELECT md5(g::text) AS v"
                " FROM generate_series(1, 1000000) AS g"
            ).format(name)
        )
    table = f"{database.schema}.hash_large"
    with pytest.raises(psycopg.errors.QueryCanceled):
        plan(database.dsn, table, "v", 4, method="hash", statement_timeout_ms=1)
