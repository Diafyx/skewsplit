"""Hash plans against a fake connection, and choosing the method.

The query itself runs against every supported PostgreSQL version in
tests/integration; these tests cover how it is built and how its result is
read.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.postgres import hashing, planning
from skewsplit.postgres.catalog import Column
from skewsplit.postgres.hashing import count_rows, plan_from_hash

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
        self,
        query: object,
        params: dict[str, object] | None = None,
        *,
        binary: bool = False,  # noqa: ARG002 -- the caller-SQL queries pass True
    ) -> FakeCursor:
        text = query.as_string(None) if isinstance(query, sql.Composable) else str(query)
        self.conn.queries.append((text, params))
        self.result = self.conn.results.pop(0)
        return self

    def fetchone(self) -> object:
        return self.result


class FakeConnection:
    """Answers each query with the next result, and records each transaction."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.queries: list[tuple[str, dict[str, object] | None]] = []
        self.rollbacks: list[bool] = []

    def cursor(self, *, row_factory: object) -> FakeCursor:
        assert row_factory is tuple_row
        return FakeCursor(self)

    @contextmanager
    def transaction(self, *, force_rollback: bool = False) -> Iterator[None]:
        self.rollbacks.append(force_rollback)
        yield


QUALIFIED = 'sales."Orders"'
COLUMN = Column("sales", "Orders", "Session", QUALIFIED, "uuid", None, 16384)
# find_column's row for a uuid column, which the planner cannot split by range.
FIND_COLUMN_ROW = (16384, "r", "sales", "Orders", QUALIFIED, False, 2950, -1, "uuid", True)


def conn_of(fake: FakeConnection) -> psycopg.Connection[Any]:
    return cast("psycopg.Connection[Any]", fake)


def test_one_query_counts_the_rows_in_a_read_only_transaction() -> None:
    fake = FakeConnection(None, None, (1010, 1000))
    assert count_rows(conn_of(fake), COLUMN, statement_timeout_ms=500) == (1010, 1000)
    assert fake.rollbacks == [True]
    assert fake.queries == [
        ("SELECT pg_catalog.set_config('transaction_read_only', 'on', true)", None),
        (
            "SELECT pg_catalog.set_config('statement_timeout', %(timeout)s, true)",
            {"timeout": "500"},
        ),
        ('SELECT count(*), count("Session") FROM sales."Orders"', {}),
    ]


def test_without_a_timeout_the_session_keeps_its_own() -> None:
    fake = FakeConnection(None, (0, 0))
    assert count_rows(conn_of(fake), COLUMN) == (0, 0)
    assert len(fake.queries) == 2


def test_the_timeout_is_checked_before_any_query() -> None:
    fake = FakeConnection()
    with pytest.raises(ValueError, match="statement_timeout_ms must be positive, not 0"):
        count_rows(conn_of(fake), COLUMN, statement_timeout_ms=0)
    assert fake.queries == []


def test_a_hash_plan_from_the_counts() -> None:
    fake = FakeConnection(FIND_COLUMN_ROW, None, (1010, 1000))
    result = plan_from_hash(conn_of(fake), QUALIFIED, "Session", 4)
    assert (result.type_name, result.null_rows, result.total_rows) == ("uuid", 10, 1010)
    assert [part.estimated_rows for part in result.parts] == [10, 250, 250, 250, 250]
    assert result.predicates[1] == (
        '((pg_catalog.hashtextextended("Session"::text, 0) % 4) + 4) % 4 = 0'
    )
    source: dict[str, Any] = result.source  # type: ignore[assignment]
    assert {key: source[key] for key in ("statistics", "table", "rows")} == {
        "statistics": "count",
        "table": QUALIFIED,
        "rows": 1010,
    }
    assert isinstance(source["planning_ms"], float)
    assert [warning.code for warning in result.warnings] == ["full_scans"]


def test_the_planning_time_is_rounded_to_microseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([10.0, 10.0123456])
    monkeypatch.setattr(hashing, "perf_counter", lambda: next(times))
    fake = FakeConnection(FIND_COLUMN_ROW, None, (4, 4))
    result = plan_from_hash(conn_of(fake), QUALIFIED, "Session", 2)
    assert result.source["planning_ms"] == 12.346  # type: ignore[index]


def test_the_timeout_is_checked_before_connecting() -> None:
    with pytest.raises(ValueError, match="statement_timeout_ms must be positive, not -1"):
        plan_from_hash("dsn", "t", "c", 4, statement_timeout_ms=-1)


# Choosing the method


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    seen: list[tuple[object, ...]] = []
    for name in ("plan_from_hash", "plan_from_statistics", "plan_from_sample"):
        monkeypatch.setattr(
            planning, name, lambda *args, _name=name, **kw: seen.append((_name, args, kw))
        )
    return seen


def test_the_hash_method_passes_the_timeout(calls: list[tuple[object, ...]]) -> None:
    planning.plan("dsn", "t", "c", 4, method="hash", statement_timeout_ms=9)
    planning.plan("dsn", "t", "c", 4, method="hash")
    assert calls == [
        ("plan_from_hash", ("dsn", "t", "c", 4), {"statement_timeout_ms": 9, "where": None}),
        ("plan_from_hash", ("dsn", "t", "c", 4), {"statement_timeout_ms": None, "where": None}),
    ]


def test_range_is_the_default_method(calls: list[tuple[object, ...]]) -> None:
    planning.plan("dsn", "t", "c", 4, method="range", strategy="statistics")
    planning.plan("dsn", "t", "c", 4, method="range", strategy="sample", seed=3)
    assert [call[0] for call in calls] == ["plan_from_statistics", "plan_from_sample"]


@pytest.mark.parametrize(
    "options",
    [
        {"strategy": "statistics"},
        {"strategy": "exact"},
        {"sample_rows": 5},
        {"max_sample_rows": 5},
        {"seed": 0},
    ],
)
def test_range_options_do_not_fit_the_hash_method(
    calls: list[tuple[object, ...]], options: dict[str, Any]
) -> None:
    with pytest.raises(
        ValueError,
        match="strategy, sample_rows, max_sample_rows and seed are for method='range'",
    ):
        planning.plan("dsn", "t", "c", 4, method="hash", **options)
    assert calls == []


def test_an_unknown_method(calls: list[tuple[object, ...]]) -> None:
    with pytest.raises(ValueError, match="method must be one of range, hash, not 'mod'"):
        planning.plan("dsn", "t", "c", 4, method="mod")  # type: ignore[arg-type]
    assert calls == []


def test_an_empty_strategy_is_not_the_default(calls: list[tuple[object, ...]]) -> None:
    with pytest.raises(ValueError, match="strategy must be one of statistics, sample, exact"):
        planning.plan("dsn", "t", "c", 4, strategy="")  # type: ignore[arg-type]
    assert calls == []
