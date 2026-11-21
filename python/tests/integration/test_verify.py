from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.pq import TransactionStatus

from skewsplit.postgres.statistics import plan_from_statistics
from skewsplit.postgres.verify import Verification, verify, verify_plan

if TYPE_CHECKING:
    from integration.conftest import Database

# 900 rows with values 1 to 900, and 100 NULL rows.
SETUP = """
CREATE TABLE {t} (v integer) WITH (autovacuum_enabled = false);
INSERT INTO {t} SELECT CASE WHEN g <= 900 THEN g END FROM generate_series(1, 1000) AS g;
ANALYZE {t};
CREATE SEQUENCE {seq};
CREATE TABLE {victim} (id integer)
"""


@pytest.fixture(scope="module")
def table(database: Database) -> str:
    names = {
        "t": sql.Identifier(database.schema, "verified"),
        "seq": sql.Identifier(database.schema, "verified_seq"),
        "victim": sql.Identifier(database.schema, "verified_victim"),
    }
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        for statement in SETUP.split(";\n"):
            admin.execute(sql.SQL(statement).format(**names))
    return f"{database.schema}.verified"


def check(conn: psycopg.Connection[Any], table: str, *predicates: str) -> Verification:
    return verify(conn, table, "v", list(predicates), counts=True)


def test_a_plan_from_the_statistics_is_correct(conn: psycopg.Connection[Any], table: str) -> None:
    planned = plan_from_statistics(conn, table, "v", 4)
    report: Any = verify_plan(conn, table, planned, counts=True)
    assert report["ok"] is True
    assert report["rows"] == {"total": 1000, "unmatched": 0, "overlapping": 0}
    actual = [entry["actual_rows"] for entry in report["predicates"]]
    assert sum(actual) == 1000
    assert actual[0] == 100
    skew = report["skew_score"]
    assert skew["actual"] == (2 * max(actual) * 4 * 1000 + 1000) // 2000 / 1000
    # ANALYZE read every row: 100 buckets of 9 rows, so spec section 13 bounds
    # the difference by 2 * 9 * 4 / 1000 + 0.001.
    assert abs(skew["actual"] - skew["estimated"]) <= 0.073 + 1e-9


def test_an_overlapping_plan(conn: psycopg.Connection[Any], table: str) -> None:
    found = check(conn, table, '"v" IS NULL', '"v" < 500', '"v" >= 400')
    assert not found.ok
    assert (found.total_rows, found.unmatched_rows, found.overlapping_rows) == (1000, 0, 100)
    assert found.predicate_rows == (100, 499, 501)


def test_a_plan_with_a_gap(conn: psycopg.Connection[Any], table: str) -> None:
    found = check(conn, table, '"v" IS NULL', '"v" < 500', '"v" > 500')
    assert not found.ok
    assert (found.unmatched_rows, found.overlapping_rows) == (1, 0)
    assert found.predicate_rows == (100, 499, 400)


def test_a_predicate_that_is_null_does_not_match(conn: psycopg.Connection[Any], table: str) -> None:
    # Without an IS NULL predicate, the comparisons are NULL for the NULL rows.
    found = check(conn, table, '"v" < 500', '"v" >= 500')
    assert (found.unmatched_rows, found.overlapping_rows) == (100, 0)


def test_a_statement_timeout(database: Database, table: str) -> None:
    with psycopg.connect(database.dsn) as caller:
        caller.execute("SELECT 1")
        with pytest.raises(errors.QueryCanceled):
            verify(
                caller,
                table,
                "v",
                ['"v" IS NULL', "(SELECT true FROM pg_sleep(2))"],
                statement_timeout_ms=100,
            )
        # The caller's transaction and settings are as they were.
        assert caller.info.transaction_status == TransactionStatus.INTRANS
        settings = caller.execute(
            "SELECT current_setting('statement_timeout'), current_setting('transaction_read_only')"
        ).fetchone()
        assert settings == ("0", "off")


def test_predicates_cannot_write(conn: psycopg.Connection[Any], table: str) -> None:
    sequence = table + "_seq"
    with pytest.raises(errors.ReadOnlySqlTransaction):
        check(conn, table, f"nextval('{sequence}') > 0")
    row = conn.execute(sql.SQL("SELECT is_called FROM {}").format(sql.SQL(sequence))).fetchone()
    assert row == (False,)


def test_a_predicate_cannot_run_another_statement(
    conn: psycopg.Connection[Any], table: str
) -> None:
    victim = table + "_victim"
    # Valid SQL that, pasted into the scan, makes three statements, the second
    # a DROP TABLE. PostgreSQL refuses to run more than one.
    injection = (
        f"true)]::boolean[], true)) AS matches FROM {table}) AS t;\n"
        f"DROP TABLE {victim};\n"
        "SELECT count(*) FROM (SELECT cardinality(array_positions(ARRAY[(true"
    )
    with pytest.raises(errors.SyntaxError, match="multiple commands"):
        verify(conn, table, "v", [injection])
    assert conn.execute("SELECT to_regclass(%s) IS NOT NULL", (victim,)).fetchone() == (True,)


def test_a_connection_from_a_dsn(database: Database, table: str) -> None:
    found = verify(database.dsn, table, "v", ['"v" IS NULL', '"v" IS NOT NULL'])
    assert found == Verification(table, 1000, 0, 0, None)
