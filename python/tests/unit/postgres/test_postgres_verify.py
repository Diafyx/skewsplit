"""Verify mode against a fake connection.

The queries themselves run against every supported PostgreSQL version in
tests/integration; these tests cover how they are built and read.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.core.planner import plan
from skewsplit.postgres.verify import Verification, verify, verify_plan

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg
    from typing_extensions import Self


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.result: object = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def execute(
        self, query: object, params: object = None, *, binary: bool | None = None
    ) -> FakeCursor:
        text = query.as_string(None) if isinstance(query, sql.Composable) else query
        self.conn.queries.append((cast("str", text), params, binary))
        self.result = self.conn.results.pop(0)
        return self

    def fetchone(self) -> object:
        return self.result


class FakeConnection:
    """Answers each query with the next result, and records each transaction."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.queries: list[tuple[str, object, bool | None]] = []
        self.rollbacks: list[bool] = []

    def cursor(self, *, row_factory: object) -> FakeCursor:
        assert row_factory is tuple_row
        return FakeCursor(self)

    @contextmanager
    def transaction(self, *, force_rollback: bool = False) -> Iterator[None]:
        self.rollbacks.append(force_rollback)
        yield


QUALIFIED = 'sales."Orders"'
FIND_COLUMN_ROW = (16384, "r", "sales", "Orders", QUALIFIED, False, 23, -1, "integer", True)
PREDICATES = ['"id" IS NULL', '"id" < 42', '"id" >= 42 -- the rest']


def run(
    *results: object, counts: bool = False, statement_timeout_ms: int | None = None
) -> tuple[FakeConnection, Verification]:
    """Verify PREDICATES; ``results`` answer the queries after find_column's."""
    fake = FakeConnection(FIND_COLUMN_ROW, *results)
    conn = cast("psycopg.Connection[Any]", fake)
    found = verify(
        conn,
        QUALIFIED,
        "id",
        PREDICATES,
        counts=counts,
        statement_timeout_ms=statement_timeout_ms,
    )
    return fake, found


def test_one_scan_checks_every_row() -> None:
    fake, found = run(None, (100, 0, 0))
    assert found == Verification(QUALIFIED, 100, 0, 0, None)
    assert found.ok
    _, read_only, scan = fake.queries
    assert read_only == (
        "SELECT pg_catalog.set_config('transaction_read_only', 'on', true)",
        None,
        None,
    )
    text, params, binary = scan
    # Each predicate on lines of its own, so the comment ends with it.
    assert text == (
        "\nSELECT count(*), count(*) FILTER (WHERE matches = 0),"
        " count(*) FILTER (WHERE matches > 1)\n"
        "FROM (\n"
        "  SELECT pg_catalog.cardinality(pg_catalog.array_positions(ARRAY[(\n"
        '"id" IS NULL\n), (\n"id" < 42\n), (\n"id" >= 42 -- the rest\n'
        ")]::boolean[], true))\n"
        "         AS matches\n"
        '  FROM sales."Orders"\n'
        ") AS t\n"
    )
    assert (params, binary) == (None, True)
    # find_column's savepoint is kept; the checking transaction is rolled back.
    assert fake.rollbacks == [False, True]


@pytest.mark.parametrize(
    ("unmatched", "overlapping"),
    [(1, 0), (0, 3), (2, 5)],
)
def test_rows_matched_by_no_predicate_or_several(unmatched: int, overlapping: int) -> None:
    _, found = run(None, (100, unmatched, overlapping))
    assert (found.unmatched_rows, found.overlapping_rows) == (unmatched, overlapping)
    assert not found.ok


def test_counts_take_one_query_per_predicate() -> None:
    fake, found = run(None, (100, 0, 0), (10,), (30,), (60,), counts=True)
    assert found.predicate_rows == (10, 30, 60)
    counts = fake.queries[3:]
    prefix = 'SELECT count(*) FROM sales."Orders" WHERE (\n'
    assert all(query.startswith(prefix) for query, _, _ in counts)
    assert [query[len(prefix) :] for query, _, _ in counts] == [f"{p}\n)" for p in PREDICATES]
    assert {(params, binary) for _, params, binary in counts} == {(None, True)}


def test_a_statement_timeout() -> None:
    fake, _ = run(None, None, (5, 0, 0), statement_timeout_ms=5000)
    query, params, _ = fake.queries[2]
    assert query == "SELECT pg_catalog.set_config('statement_timeout', %(timeout)s, true)"
    assert params == {"timeout": "5000"}


@pytest.mark.parametrize("timeout", [0, -1])
def test_the_timeout_must_be_positive(timeout: int) -> None:
    fake = FakeConnection()
    conn = cast("psycopg.Connection[Any]", fake)
    with pytest.raises(ValueError, match="statement_timeout_ms must be positive"):
        verify(conn, QUALIFIED, "id", PREDICATES, statement_timeout_ms=timeout)
    assert fake.queries == []


def test_the_actual_skew_score() -> None:
    # The plan of spec section 5: its largest predicate has 500 of 1060 rows.
    found = Verification(QUALIFIED, 1060, 0, 0, (100, 76, 500, 192, 192))
    assert found.skew_milli(4) == 1887
    assert Verification(QUALIFIED, 1060, 0, 0).skew_milli(4) is None
    assert Verification(QUALIFIED, 0, 0, 0, (0, 0)).skew_milli(4) is None
    assert Verification(QUALIFIED, 5, 5, 0, ()).skew_milli(4) == 0


SECTION_1_INPUT = {
    "column": "id",
    "type": "int4",
    "partitions": 4,
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
}


def test_verify_plan_reports_actual_rows_next_to_the_estimates() -> None:
    planned = plan(SECTION_1_INPUT)
    fake = FakeConnection(FIND_COLUMN_ROW, None, (1060, 0, 0), 100, 80, 500, 190, 190)
    fake.results[3:] = [(rows,) for rows in fake.results[3:]]
    report = verify_plan(cast("psycopg.Connection[Any]", fake), QUALIFIED, planned, counts=True)
    assert report == {
        "ok": True,
        "table": QUALIFIED,
        "rows": {"total": 1060, "unmatched": 0, "overlapping": 0},
        "predicates": [
            {"sql": predicate.sql, "estimated_rows": predicate.estimated_rows, "actual_rows": rows}
            for predicate, rows in zip(planned.parts, [100, 80, 500, 190, 190])
        ],
        "skew_score": {"estimated": 1.887, "actual": 1.887},
    }


def test_verify_plan_without_counts() -> None:
    planned = plan(SECTION_1_INPUT)
    fake = FakeConnection(FIND_COLUMN_ROW, None, None, (1060, 2, 0))
    conn = cast("psycopg.Connection[Any]", fake)
    report = verify_plan(conn, QUALIFIED, planned, statement_timeout_ms=100)
    assert report["ok"] is False
    assert report["rows"] == {"total": 1060, "unmatched": 2, "overlapping": 0}
    predicates: Any = report["predicates"]
    assert {entry["actual_rows"] for entry in predicates} == {None}
    assert report["skew_score"] == {"estimated": 1.887, "actual": None}
    assert fake.queries[2][1] == {"timeout": "100"}
