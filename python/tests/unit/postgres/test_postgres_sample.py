"""Sampling a column against a fake connection, and choosing the strategy.

The queries themselves run against every supported PostgreSQL version in
tests/integration; these tests cover how they are built and how their results
are read.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pytest
from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.postgres import planning, sample, statistics
from skewsplit.postgres.catalog import Column
from skewsplit.postgres.sample import (
    Sample,
    plan_from_sample,
    read_sample,
    sample_distribution,
    sampling_method,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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

    def execute(self, query: object, params: dict[str, object] | None = None) -> FakeCursor:
        text = query.as_string(None) if isinstance(query, sql.Composable) else str(query)
        self.conn.queries.append((text, params))
        self.result = self.conn.results.pop(0)
        return self

    def fetchone(self) -> object:
        return self.result

    def fetchall(self) -> object:
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
COLUMN = Column(
    schema="sales",
    table="Orders",
    name="id",
    qualified=QUALIFIED,
    type_name="int4",
    scale=None,
    relid=16384,
)
FIND_COLUMN_ROW = (16384, "r", "sales", "Orders", QUALIFIED, False, 23, -1, "integer", True)
COUNTED = [(None, 2), ("42", 6), ("7", 1), ("12", 2), ("30", 1), ("1", 1), ("20", 1), ("3", 1)]


def explained(rows: object) -> tuple[object]:
    return ([{"Plan": {"Node Type": "Seq Scan", "Plan Rows": rows}}],)


def conn_of(fake: FakeConnection) -> psycopg.Connection[Any]:
    return cast("psycopg.Connection[Any]", fake)


def read(*results: object, **options: int) -> tuple[FakeConnection, Sample]:
    """Sample COLUMN; ``results`` answer the settings, estimate and sample queries."""
    fake = FakeConnection(None, *results)
    return fake, read_sample(conn_of(fake), COLUMN, **options)


# Choosing the method


@pytest.mark.parametrize(
    ("planner_rows", "expected"),
    [
        (1, ("all", None)),
        (30000, ("all", None)),
        (30001, ("bernoulli", 100 * 30000 / 30001)),
        (300000, ("bernoulli", 10.0)),
        (300001, ("system", 100 * 30000 / 300001)),
        (10**7, ("system", 0.3)),
    ],
)
def test_the_method_follows_the_share_of_the_table_sampled(
    planner_rows: int, expected: tuple[str, float | None]
) -> None:
    assert sampling_method(planner_rows, 30000) == expected


# Reading a sample


def test_a_large_table_is_sampled_by_page_with_a_seed() -> None:
    fake, found = read(None, explained(10**6), COUNTED, seed=7, statement_timeout_ms=500)
    assert fake.rollbacks == [True]
    settings, timeout, estimate, query = fake.queries
    for setting in ("'DateStyle', 'ISO'", "'TimeZone', 'UTC'", "'transaction_read_only', 'on'"):
        assert f"set_config({setting}, true)" in settings[0]
    assert timeout == (
        "SELECT pg_catalog.set_config('statement_timeout', %(timeout)s, true)",
        {"timeout": "500"},
    )
    assert estimate == (f"EXPLAIN (FORMAT JSON) SELECT FROM {QUALIFIED}", None)
    assert " ".join(query[0].split()) == (
        'SELECT s.v::text, count(*) FROM (SELECT "id" AS v FROM sales."Orders"'
        " TABLESAMPLE SYSTEM (%(percent)s) REPEATABLE (%(seed)s) LIMIT %(limit)s) AS s"
        " GROUP BY s.v"
    )
    assert query[1] == {"limit": 300000, "percent": 3.0, "seed": 7}
    assert found == Sample(
        column=COLUMN,
        method="system",
        percent=3.0,
        seed=7,
        planner_rows=10**6,
        limit=300000,
        null_rows=2,
        values=tuple(pair for pair in COUNTED if pair[0] is not None),  # type: ignore[misc]
    )


def test_a_mid_sized_table_is_sampled_by_row() -> None:
    fake, found = read(explained(100000), COUNTED, sample_rows=20000, max_sample_rows=20000)
    query, params = fake.queries[-1]
    assert " TABLESAMPLE BERNOULLI (%(percent)s) LIMIT %(limit)s)" in query
    assert params == {"limit": 20000, "percent": 20.0}
    assert (found.method, found.percent, found.seed, found.limit) == (
        "bernoulli",
        20.0,
        None,
        20000,
    )


def test_a_small_table_is_read_whole_and_the_seed_is_unused() -> None:
    fake, found = read(explained(30000), [(None, 3)], seed=7)
    query, params = fake.queries[-1]
    assert "TABLESAMPLE" not in query
    assert "REPEATABLE" not in query
    assert 'FROM sales."Orders" LIMIT %(limit)s) AS s' in " ".join(query.split())
    assert params == {"limit": 300000}
    assert (found.method, found.percent, found.seed) == ("all", None, None)
    assert (found.null_rows, found.values, found.rows) == (3, (), 3)
    assert len(fake.queries) == 3


@pytest.mark.parametrize(
    ("plan_rows", "expected"),
    [(0, 1), (0.4, 1), (2.5, 3), (2550, 2550), (1.5e7, 15000000)],
)
def test_the_planner_estimate_is_a_whole_number_of_at_least_one(
    plan_rows: float, expected: int
) -> None:
    _, found = read(explained(plan_rows), [])
    assert found.planner_rows == expected


def test_the_estimate_may_come_as_json_text() -> None:
    document = json.dumps(explained(123456)[0])
    _, found = read((document,), [], sample_rows=1000)
    assert (found.planner_rows, found.method) == (123456, "system")


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"sample_rows": 0}, "sample_rows must be positive, not 0"),
        ({"sample_rows": -5}, "sample_rows must be positive, not -5"),
        ({"sample_rows": 100, "max_sample_rows": 99}, "must be at least sample_rows (100), not 99"),
        ({"statement_timeout_ms": 0}, "statement_timeout_ms must be positive, not 0"),
    ],
)
def test_options_out_of_range(options: dict[str, Any], message: str) -> None:
    fake = FakeConnection()
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        read_sample(conn_of(fake), COLUMN, **options)
    assert fake.queries == []


@pytest.mark.parametrize(("sample_rows", "limit"), [(1, 10), (30000, 300000), (7, 7)])
def test_the_limit_is_ten_times_the_sample_size_unless_given(sample_rows: int, limit: int) -> None:
    options = {"max_sample_rows": 7} if sample_rows == 7 else {}
    _, found = read(explained(10), [], sample_rows=sample_rows, **options)
    assert found.limit == limit


# What a sample estimates


BASE = Sample(
    column=COLUMN,
    method="system",
    percent=40.0,
    seed=None,
    planner_rows=1000,
    limit=100,
    null_rows=1,
    values=(("5", 2),),
)


@pytest.mark.parametrize(
    ("fields", "rows", "reached", "estimate"),
    [
        ({}, 3, False, 8),  # 3 x 100 / 40 = 7.5 rounds up
        ({"percent": 30.0}, 3, False, 10),
        ({"percent": 70.0}, 3, False, 4),  # 4.29 rounds down
        ({"method": "all", "percent": None}, 3, False, 3),
        ({"limit": 3}, 3, True, 1000),
        ({"limit": 3, "planner_rows": 2}, 3, True, 3),
        ({"null_rows": 0, "values": ()}, 0, False, 0),
    ],
)
def test_the_row_estimate(
    fields: dict[str, Any], rows: int, *, reached: bool, estimate: int
) -> None:
    found = replace(BASE, **fields)
    assert (found.rows, found.reached_limit, found.rows_estimate) == (rows, reached, estimate)


def test_the_conversion_input_and_the_report() -> None:
    found = replace(BASE, column=Column("s", "t", "amount", "s.t", "numeric", 2, 1), seed=3)
    assert found.conversion_input(5) == {
        "column": "amount",
        "type": "numeric",
        "scale": 2,
        "partitions": 5,
        "rows_estimate": 8,
        "sample_null_rows": 1,
        "sample_values": [{"value": "5", "rows": 2}],
    }
    assert "scale" not in BASE.conversion_input(5)
    assert found.report() == {
        "table": "s.t",
        "method": "system",
        "percent": 40.0,
        "seed": 3,
        "planner_rows": 1000,
        "sample_limit": 100,
    }
    assert found.warnings() == []
    [warning] = replace(BASE, limit=3).warnings()
    assert (warning["code"], warning["rows"]) == ("sample_limit_reached", 3)
    assert "stopped at its limit of 3 rows" in str(warning["message"])


# The distribution and the plan


def test_the_distribution_from_a_sample() -> None:
    fake = FakeConnection(FIND_COLUMN_ROW, None, explained(10**6), COUNTED)
    data = sample_distribution(conn_of(fake), QUALIFIED, "id", sample_rows=1000, seed=9)
    assert "partitions" not in data
    # Each of the 15 sampled rows stands for 1000.
    assert data["mcv"] == [{"value": "12", "rows": 2000}, {"value": "42", "rows": 6000}]
    assert data["source"] == {
        "statistics": "sample",
        # 15 sampled rows at 0.1%: 15000.
        "rows_estimate": 15000,
        "sample_rows": 15,
        "unestimated_rows": 0,
        "warnings": [],
        "table": QUALIFIED,
        "method": "system",
        "percent": 0.1,
        "seed": 9,
        "planner_rows": 10**6,
        "sample_limit": 10000,
    }


def test_the_limit_warning_comes_after_those_of_the_conversion() -> None:
    column = Column("s", "t", "d", "s.t", "date", None, 1)
    counted = [("infinity", 2), ("2026-01-01", 1), ("2026-01-02", 1)]
    fake = FakeConnection(None, explained(10), counted)
    found = read_sample(conn_of(fake), column, sample_rows=4, max_sample_rows=4)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sample, "find_column", lambda *_: column)
        patch.setattr(sample, "read_sample", lambda *_, **__: found)
        data = sample_distribution(conn_of(FakeConnection()), "s.t", "d")
    source: dict[str, Any] = data["source"]  # type: ignore[assignment]
    assert [warning["code"] for warning in source["warnings"]] == [
        "special_values_left_out",
        "sample_limit_reached",
    ]
    # The limit of 4 was reached, so T is the larger of E = 10 and n = 4.
    assert source["rows_estimate"] == 10


def test_the_plan_from_a_sample_reports_the_planning_time() -> None:
    fake = FakeConnection(FIND_COLUMN_ROW, None, explained(1000), COUNTED)
    result = plan_from_sample(conn_of(fake), QUALIFIED, "id", 4, sample_rows=1000)
    source: dict[str, Any] = result.source  # type: ignore[assignment]
    assert (source["method"], source["rows_estimate"], source["sample_rows"]) == ("all", 15, 15)
    assert isinstance(source["planning_ms"], float)
    assert source["planning_ms"] >= 0
    assert result.predicates[0] == '"id" IS NULL'
    assert result.report.splitlines()[1].startswith("from a sample of 15 rows, planned in ")


# Choosing the strategy


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    seen: list[tuple[object, ...]] = []
    for name in ("plan_from_sample", "sample_distribution", "plan_from_statistics"):
        monkeypatch.setattr(
            planning, name, lambda *args, _name=name, **kw: seen.append((_name, args, kw))
        )
    monkeypatch.setattr(
        planning,
        "statistics_distribution",
        lambda *args, **kw: seen.append(("statistics_distribution", args, kw)),
    )
    return seen


def test_statistics_is_the_default_strategy(calls: list[tuple[object, ...]]) -> None:
    planning.plan("dsn", "t", "c", 4, statement_timeout_ms=9)
    planning.distribution("dsn", "t", "c")
    assert calls == [
        ("plan_from_statistics", ("dsn", "t", "c", 4), {"statement_timeout_ms": 9}),
        ("statistics_distribution", ("dsn", "t", "c"), {"statement_timeout_ms": None}),
    ]


def test_the_sample_strategy_passes_its_options(calls: list[tuple[object, ...]]) -> None:
    planning.plan("dsn", "t", "c", 4, strategy="sample", seed=1)
    planning.distribution(
        "dsn", "t", "c", strategy="sample", sample_rows=5, max_sample_rows=6, statement_timeout_ms=7
    )
    options = {"max_sample_rows": None, "seed": 1, "statement_timeout_ms": None}
    assert calls == [
        ("plan_from_sample", ("dsn", "t", "c", 4), {"sample_rows": 30000, **options}),
        (
            "sample_distribution",
            ("dsn", "t", "c"),
            {"sample_rows": 5, "max_sample_rows": 6, "seed": None, "statement_timeout_ms": 7},
        ),
    ]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (
            {"strategy": "fast"},
            "strategy must be one of statistics, sample, exact, auto, not 'fast'",
        ),
        ({"sample_rows": 5}, "sample_rows, max_sample_rows and seed are for strategy='sample'"),
        ({"max_sample_rows": 5}, "are for strategy='sample'"),
        ({"seed": 0}, "are for strategy='sample'"),
    ],
)
def test_options_that_do_not_fit_the_strategy(
    calls: list[tuple[object, ...]], options: dict[str, Any], message: str
) -> None:
    for function in (planning.distribution, planning.plan):
        arguments = ("dsn", "t", "c") if function is planning.distribution else ("dsn", "t", "c", 4)
        with pytest.raises(ValueError, match=message):
            function(*arguments, **options)
    assert calls == []


# The timeout of the statistics strategy


def test_the_statistics_are_read_with_the_timeout() -> None:
    table = [("r", "2000", QUALIFIED)]
    facts = ("2026-11-20 08:00:00.5+00", 3600, 150)
    row = ("0.05", "{42,7}", "{0.25,0.03}", "{1,250,500,750,1000}", -0.5)
    fake = FakeConnection(None, None, table, facts, row)
    statistics.read_statistics(conn_of(fake), COLUMN, statement_timeout_ms=250)
    assert fake.queries[1] == (
        "SELECT pg_catalog.set_config('statement_timeout', %(timeout)s, true)",
        {"timeout": "250"},
    )


@pytest.mark.parametrize("function", [statistics.distribution, statistics.plan_from_statistics])
def test_the_statistics_strategy_checks_the_timeout_first(
    function: Callable[..., object],
) -> None:
    arguments = ("dsn", "t", "c") if function is statistics.distribution else ("dsn", "t", "c", 4)
    with pytest.raises(ValueError, match="statement_timeout_ms must be positive, not -1"):
        function(*arguments, statement_timeout_ms=-1)
