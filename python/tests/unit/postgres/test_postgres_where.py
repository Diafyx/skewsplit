"""The caller's filter (spec section 18) in every query that reads the table.

These tests use a fake connection, which records each query as psycopg would
compose it, its parameters, and whether it ran with the extended protocol.
tests/integration runs the queries against PostgreSQL.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.postgres import statistics
from skewsplit.postgres.auto import auto_distribution, choose, plan_auto
from skewsplit.postgres.catalog import Column
from skewsplit.postgres.connection import caller_sql, run, where_clause
from skewsplit.postgres.exact import exact_distribution, plan_from_exact, read_quantiles
from skewsplit.postgres.hashing import count_rows, plan_from_hash
from skewsplit.postgres.sample import plan_from_sample, read_sample, sample_distribution
from skewsplit.postgres.verify import verify

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg
    import pytest
    from typing_extensions import Self

FILTER = "created_at >= '2026-01-01' AND note LIKE 'a%' -- recent"
# How each query holds the filter: on lines of its own, with its % doubled for
# psycopg, which turns it back into one when it sends the query.
HELD = "(\ncreated_at >= '2026-01-01' AND note LIKE 'a%%' -- recent\n)"


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.result: object = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def execute(
        self, query: object, params: dict[str, object] | None = None, *, binary: bool = False
    ) -> FakeCursor:
        text = query.as_string(None) if isinstance(query, sql.Composable) else str(query)
        self.conn.queries.append((text, params, binary))
        self.result = self.conn.results.pop(0)
        return self

    def fetchone(self) -> object:
        return self.result

    def fetchall(self) -> object:
        return self.result


class FakeConnection:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.queries: list[tuple[str, dict[str, object] | None, bool]] = []

    def cursor(self, *, row_factory: object) -> FakeCursor:
        assert row_factory is tuple_row
        return FakeCursor(self)

    @contextmanager
    def transaction(self, *, force_rollback: bool = False) -> Iterator[None]:  # noqa: ARG002
        yield


QUALIFIED = 'sales."Orders"'
COLUMN = Column("sales", "Orders", "id", QUALIFIED, "int4", None, 16384)
FIND_COLUMN_ROW = (16384, "r", "sales", "Orders", QUALIFIED, False, 23, -1, "integer", True)


def conn_of(fake: FakeConnection) -> psycopg.Connection[Any]:
    return cast("psycopg.Connection[Any]", fake)


def caller_queries(fake: FakeConnection) -> list[tuple[str, dict[str, object] | None, bool]]:
    """The queries that hold caller SQL."""
    return [query for query in fake.queries if HELD in query[0]]


def test_caller_sql_is_on_lines_of_its_own_with_its_percent_signs_doubled() -> None:
    assert caller_sql(FILTER).as_string(None) == HELD
    assert where_clause(FILTER).as_string(None) == f" WHERE {HELD}"
    assert where_clause(None).as_string(None) == ""


def test_run_always_uses_the_extended_protocol() -> None:
    fake = FakeConnection(None, None)
    cursor = cast("psycopg.Cursor[Any]", FakeCursor(fake))
    run(cursor, sql.SQL("SELECT 1"))
    run(cursor, sql.SQL("SELECT %(x)s"), {"x": 1})
    assert fake.queries == [("SELECT 1", {}, True), ("SELECT %(x)s", {"x": 1}, True)]


def test_the_sample_and_its_estimate_are_of_the_filtered_rows() -> None:
    explained = ([{"Plan": {"Plan Rows": 1000}}],)
    fake = FakeConnection(None, explained, [("7", 3)])
    found = read_sample(conn_of(fake), COLUMN, where=FILTER)
    estimate, sample = caller_queries(fake)
    assert estimate == (f"EXPLAIN (FORMAT JSON) SELECT FROM {QUALIFIED} WHERE {HELD}", {}, True)
    assert f'SELECT "id" AS v FROM {QUALIFIED} WHERE {HELD} LIMIT %(limit)s' in sample[0]  # noqa: S608 -- expected SQL text, never run
    assert sample[1:] == ({"limit": 300000}, True)
    assert found.report()["where"] == FILTER


def test_the_filter_comes_after_tablesample() -> None:
    explained = ([{"Plan": {"Plan Rows": 10**6}}],)
    fake = FakeConnection(None, explained, [])
    read_sample(conn_of(fake), COLUMN, seed=3, where=FILTER)
    query = caller_queries(fake)[1][0]
    assert f"TABLESAMPLE SYSTEM (%(percent)s) REPEATABLE (%(seed)s) WHERE {HELD} LIMIT" in query


def test_a_sample_without_a_filter_says_nothing_of_one() -> None:
    explained = ([{"Plan": {"Plan Rows": 10}}],)
    fake = FakeConnection(None, explained, [])
    assert "where" not in read_sample(conn_of(fake), COLUMN).report()


def test_the_distribution_from_a_filtered_sample_names_the_filter() -> None:
    explained = ([{"Plan": {"Plan Rows": 10}}],)
    fake = FakeConnection(FIND_COLUMN_ROW, None, explained, [("7", 3)])
    data = sample_distribution(conn_of(fake), QUALIFIED, "id", where=FILTER)
    assert data["source"]["where"] == FILTER  # type: ignore[index]


def test_every_exact_query_reads_the_filtered_rows() -> None:
    fake = FakeConnection(None, (25010, 25005, 25005), [("7", 25001)], (4, ["1", "5", "5", "9"]))
    found = read_quantiles(conn_of(fake), COLUMN, where=FILTER)
    queries = caller_queries(fake)
    assert len(queries) == 3
    for text, params, binary in queries:
        assert f'FROM (SELECT "id" AS v FROM {QUALIFIED} WHERE {HELD}) AS s' in text  # noqa: S608 -- expected SQL text, never run
        assert params is not None
        assert binary
    assert found.distribution()["source"]["where"] == FILTER  # type: ignore[index]


def test_an_exact_distribution_without_a_filter_says_nothing_of_one() -> None:
    fake = FakeConnection(None, (2, 2, 2), [("7", 2)])
    found = read_quantiles(conn_of(fake), COLUMN)
    assert "where" not in found.distribution()["source"]  # type: ignore[operator]


def test_the_hash_count_is_of_the_filtered_rows() -> None:
    fake = FakeConnection(None, (10, 9))
    assert count_rows(conn_of(fake), COLUMN, where=FILTER) == (10, 9)
    assert caller_queries(fake) == [
        (f'SELECT count(*), count("id") FROM {QUALIFIED} WHERE {HELD}', {}, True)  # noqa: S608 -- expected SQL text, never run
    ]


def test_a_filtered_hash_plan_names_the_filter() -> None:
    fake = FakeConnection(FIND_COLUMN_ROW, None, (10, 9))
    result = plan_from_hash(conn_of(fake), QUALIFIED, "id", 2, where=FILTER)
    assert result.source["where"] == FILTER  # type: ignore[index]


def test_verify_checks_and_counts_only_the_filtered_rows() -> None:
    fake = FakeConnection(FIND_COLUMN_ROW, None, (10, 0, 0), (4,), (6,))
    found = verify(
        conn_of(fake), QUALIFIED, "id", ['"id" < 5', '"id" >= 5'], counts=True, where=FILTER
    )
    assert found.predicate_rows == (4, 6)
    scan, first, second = caller_queries(fake)
    assert f"FROM {QUALIFIED} WHERE {HELD}\n) AS t" in scan[0]
    assert first[0] == f'SELECT count(*) FROM {QUALIFIED} WHERE (\n"id" < 5\n) AND {HELD}'  # noqa: S608 -- expected SQL text, never run
    assert second[0] == f'SELECT count(*) FROM {QUALIFIED} WHERE (\n"id" >= 5\n) AND {HELD}'  # noqa: S608 -- expected SQL text, never run
    assert all(binary for _, _, binary in (scan, first, second))


def test_the_statistics_warn_that_they_ignore_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {"column": "id", "source": {"statistics": "pg_stats", "warnings": ["earlier"]}}
    monkeypatch.setattr(statistics, "find_column", lambda *_: COLUMN)
    monkeypatch.setattr(statistics, "read_statistics", lambda *_, **__: None)
    monkeypatch.setattr(statistics, "distribution_from", lambda _: dict(data))
    found = statistics.distribution(conn_of(FakeConnection()), QUALIFIED, "id", where=FILTER)
    source: dict[str, Any] = found["source"]  # type: ignore[assignment]
    assert source["where"] == FILTER
    assert source["warnings"][0] == "earlier"
    assert source["warnings"][1] == statistics.filter_ignored(FILTER)
    assert statistics.filter_ignored(FILTER) == {
        "code": "filter_ignored",
        "where": FILTER,
        "message": (
            "pg_stats describes the whole table, so the estimates ignore the filter: the"
            " predicates still cover every row it keeps, but each can read more or fewer"
            " rows than its estimate; plan with strategy sample or exact for filtered rows"
        ),
    }
    unfiltered = statistics.distribution(conn_of(FakeConnection()), QUALIFIED, "id")
    assert unfiltered["source"] == data["source"]


def test_auto_reads_a_sample_of_a_filtered_read_without_the_statistics() -> None:
    fake = FakeConnection()
    choice = choose(conn_of(fake), COLUMN, where=FILTER)
    assert (choice.strategy, choice.reason) == ("sample", "filter_given")
    assert choice.message == (
        "the read is filtered, and pg_stats describes the whole table, so a sample"
        " of the rows the filter keeps is read"
    )
    assert fake.queries == []


def test_auto_still_hashes_a_type_ranges_cannot_split() -> None:
    column = Column("s", "t", "c", "s.t", "uuid", None, 1, splittable=False)
    choice = choose(conn_of(FakeConnection()), column, where=FILTER)
    assert choice.reason == "unsupported_type"


def test_the_exact_distribution_and_plan_pass_the_filter_down() -> None:
    results = (FIND_COLUMN_ROW, None, (3, 3, 3), [("7", 3)])
    fake = FakeConnection(*results)
    data = exact_distribution(conn_of(fake), QUALIFIED, "id", where=FILTER)
    assert data["source"]["where"] == FILTER  # type: ignore[index]
    assert len(caller_queries(fake)) == 2
    fake = FakeConnection(*results)
    result = plan_from_exact(conn_of(fake), QUALIFIED, "id", 2, where=FILTER)
    assert result.source["where"] == FILTER  # type: ignore[index]
    assert len(caller_queries(fake)) == 2


def test_the_plan_from_a_sample_passes_the_filter_down() -> None:
    explained = ([{"Plan": {"Plan Rows": 10}}],)
    fake = FakeConnection(FIND_COLUMN_ROW, None, explained, [("7", 3)])
    result = plan_from_sample(conn_of(fake), QUALIFIED, "id", 2, where=FILTER)
    assert result.source["where"] == FILTER  # type: ignore[index]
    assert len(caller_queries(fake)) == 2


def test_auto_passes_the_filter_to_the_sample_it_reads() -> None:
    explained = ([{"Plan": {"Plan Rows": 10}}],)
    fake = FakeConnection(FIND_COLUMN_ROW, FIND_COLUMN_ROW, None, explained, [("7", 3)])
    data = auto_distribution(conn_of(fake), QUALIFIED, "id", where=FILTER)
    source: dict[str, Any] = data["source"]  # type: ignore[assignment]
    assert (source["where"], source["auto"]["reason"]) == (FILTER, "filter_given")
    assert len(caller_queries(fake)) == 2
    fake = FakeConnection(FIND_COLUMN_ROW, FIND_COLUMN_ROW, None, explained, [("7", 3)])
    result = plan_auto(conn_of(fake), QUALIFIED, "id", 2, where=FILTER)
    assert result.source["where"] == FILTER  # type: ignore[index]
