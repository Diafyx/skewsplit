"""Every predicate can use a B-tree index on the column (spec section 5).

The minimum of each integer type is a common sentinel, and its literal is
written bare, like every integer. PostgreSQL's parser folds the minus sign
into the constant and types it by its value, so even -9223372036854775808 is
an int8 constant, and the comparison can use the index.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql

from skewsplit.postgres.planning import plan
from skewsplit.postgres.verify import verify

if TYPE_CHECKING:
    from collections.abc import Iterator

    from integration.conftest import Database

# Each table has an indexed column v of its type: 300 NULL rows, 11700 rows at
# the type's minimum, which makes it a dominant value, and 18000 other values.
MINIMUMS = {
    "smallint": "-32768",
    "integer": "-2147483648",
    "bigint": "-9223372036854775808",
}


def table_of(sql_type: str) -> str:
    return f"index_{sql_type}"


@pytest.fixture(scope="module")
def tables(database: Database) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        # At this target, ANALYZE reads all 30000 rows, so the statistics are exact.
        admin.execute("SET default_statistics_target = 100")
        for sql_type, minimum in MINIMUMS.items():
            names = {
                "t": sql.Identifier(database.schema, table_of(sql_type)),
                "type": sql.SQL(sql_type),
                # Quoted: in -9223372036854775808::bigint the cast comes before the minus.
                "minimum": sql.Literal(minimum),
            }
            for statement in (
                "CREATE TABLE {t} (v {type}) WITH (autovacuum_enabled = false)",
                (
                    "INSERT INTO {t} SELECT CASE WHEN g % 100 = 0 THEN NULL"
                    " WHEN g % 5 < 2 THEN {minimum}::{type} ELSE g END"
                    " FROM generate_series(1, 30000) AS g"
                ),
                "CREATE INDEX ON {t} (v)",
                "ANALYZE {t}",
            ):
                admin.execute(sql.SQL(statement).format(**names))


@pytest.fixture
def conn(database: Database) -> Iterator[psycopg.Connection[Any]]:
    """A superuser connection whose search_path is the test schema."""
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
        yield connection


def node_types(node: dict[str, Any]) -> list[str]:
    """The node types of a plan from EXPLAIN (FORMAT JSON), this node first."""
    found = [node["Node Type"]]
    for child in node.get("Plans", []):
        found += node_types(child)
    return found


def scans(conn: psycopg.Connection[Any], table: str, predicate: str) -> list[str]:
    """The plan nodes of a partition's query when sequential scans are discouraged."""
    query = sql.SQL("EXPLAIN (FORMAT JSON) SELECT v FROM {} WHERE {}").format(
        sql.Identifier(table), sql.SQL(predicate)
    )
    with conn.transaction(force_rollback=True):
        conn.execute("SET LOCAL enable_seqscan = off")
        row = conn.execute(query).fetchone()
    assert row is not None
    return node_types(row[0][0]["Plan"])


@pytest.mark.usefixtures("tables")
@pytest.mark.parametrize("sql_type", list(MINIMUMS))
def test_every_partition_can_use_the_index(conn: psycopg.Connection[Any], sql_type: str) -> None:
    table, minimum = table_of(sql_type), MINIMUMS[sql_type]
    result = plan(conn, table, "v", 4)
    # The minimum is the dominant value, and the ranges on each side compare with it.
    above = result.parts[3].upper
    assert above is not None
    assert result.predicates[1:4] == [
        f'"v" < {minimum}',
        f'"v" = {minimum}',
        f'"v" > {minimum} AND "v" < {above.value}',
    ]
    for predicate in result.predicates:
        assert "Seq Scan" not in scans(conn, table, predicate), predicate
    found = verify(conn, table, "v", result.predicates, counts=True)
    assert found.ok
    assert found.predicate_rows is not None
    assert found.predicate_rows[:3] == (300, 0, 11700)
    assert [part.estimated_rows for part in result.parts[:3]] == [300, 0, 11700]


def test_the_minimums_are_constants_of_their_type(conn: psycopg.Connection[Any]) -> None:
    # 9223372036854775808 alone is too large for int8, so it is numeric; with the
    # minus sign, the parser makes one constant, and it fits.
    row = conn.execute(
        "SELECT pg_typeof(9223372036854775808)::text, pg_typeof(-9223372036854775808)::text,"
        " pg_typeof(-2147483648)::text"
    ).fetchone()
    assert row == ("numeric", "bigint", "integer")
