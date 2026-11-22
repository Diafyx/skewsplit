"""Exact quantiles of a column against a fake connection, and the exact strategy.

The queries themselves run against every supported PostgreSQL version in
tests/integration; these tests cover how they are built and how their results
are read.
"""

from __future__ import annotations

from contextlib import contextmanager
from math import ceil
from typing import TYPE_CHECKING, Any, cast

import pytest
from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.core.errors import SpecError
from skewsplit.postgres import exact, planning
from skewsplit.postgres.catalog import Column
from skewsplit.postgres.exact import (
    Quantiles,
    bound_fractions,
    exact_distribution,
    least_common_rows,
    plan_from_exact,
    read_quantiles,
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
        self.conn.queries.append((" ".join(text.split()), params))
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
FROM = 'FROM (SELECT "id" AS v FROM sales."Orders") AS s'
# 25010 rows: 5 NULL, 20000 at 7, 5001 at 42, and 1, 5, 5 and 9 (spec section 15).
COUNTS = (25010, 25005, 25005)
COMMON = [("7", 20000), ("42", 5001)]
BOUNDS = (4, ["1", "5", "5", "9"])


def conn_of(fake: FakeConnection) -> psycopg.Connection[Any]:
    return cast("psycopg.Connection[Any]", fake)


def read(column: Column, *results: object, **options: int) -> tuple[FakeConnection, Quantiles]:
    """Read ``column``; ``results`` answer the queries after the settings."""
    fake = FakeConnection(None, *results)
    return fake, read_quantiles(conn_of(fake), column, **options)


# The common values and the bounds


@pytest.mark.parametrize(
    ("ordinary", "least"),
    [(0, 1), (1, 1), (10000, 1), (10001, 2), (20000, 2), (25000, 3), (10**12 + 1, 10**8 + 1)],
)
def test_a_common_value_has_a_ten_thousandth_of_the_rows(ordinary: int, least: int) -> None:
    assert least_common_rows(ordinary) == least


@pytest.mark.parametrize(
    ("rows", "fractions"),
    [
        (1, [0.5, 0.5]),
        (2, [0.25, 0.75]),
        (4, [0.125, 0.375, 0.625, 0.875]),
    ],
)
def test_the_fractions_ask_for_the_middle_of_each_position(
    rows: int, fractions: list[float]
) -> None:
    assert bound_fractions(rows) == fractions


@pytest.mark.parametrize("rows", [10000, 10001, 10002, 20002, 123457, 2**40 + 3, 2**50 - 1])
def test_percentile_disc_finds_the_positions_of_analyze(rows: int) -> None:
    fractions = bound_fractions(rows)
    buckets = min(10000, rows - 1)
    assert len(fractions) == buckets + 1
    # percentile_disc(f) is row max(1, ceil(f * rows)), counting from 1.
    found = [max(1, ceil(f * rows)) - 1 for f in fractions]
    assert found == [j * (rows - 1) // buckets for j in range(buckets + 1)]


# Reading the quantiles


def test_the_queries_count_find_the_common_values_and_read_the_bounds() -> None:
    fake, found = read(COLUMN, None, COUNTS, COMMON, BOUNDS, statement_timeout_ms=500)
    assert fake.rollbacks == [True]
    settings, timeout, counts, common, bounds = fake.queries
    for setting in ("'DateStyle', 'ISO'", "'TimeZone', 'UTC'", "'transaction_read_only', 'on'"):
        assert f"set_config({setting}, true)" in settings[0]
    assert timeout == (
        "SELECT pg_catalog.set_config('statement_timeout', %(timeout)s, true)",
        {"timeout": "500"},
    )
    assert counts == (
        f"SELECT count(*), count(s.v), count(*) FILTER (WHERE s.v IS NOT NULL) {FROM}",
        None,
    )
    assert common == (
        (
            f"SELECT s.v::text, count(*) {FROM} WHERE s.v IS NOT NULL GROUP BY s.v"
            " HAVING count(*) >= %(least)s ORDER BY s.v"
        ),
        {"least": 3},
    )
    assert bounds == (
        (
            "SELECT count(*), (pg_catalog.percentile_disc(%(fractions)s::float8[])"
            f" WITHIN GROUP (ORDER BY s.v))::text[] {FROM} WHERE s.v IS NOT NULL"
            ' AND s.v <> ALL (%(common)s::text[]::"pg_catalog"."int4"[])'
        ),
        {"fractions": [0.125, 0.375, 0.625, 0.875], "common": ["7", "42"]},
    )
    assert found == Quantiles(
        column=COLUMN,
        rows=25010,
        null_rows=5,
        special_rows=0,
        common=(("7", 20000), ("42", 5001)),
        histogram_rows=4,
        bounds=("1", "5", "5", "9"),
    )


def test_without_a_timeout_the_session_keeps_its_own() -> None:
    fake, _ = read(COLUMN, COUNTS, COMMON, BOUNDS)
    assert [query for query, _ in fake.queries if "statement_timeout" in query] == []
    assert len(fake.queries) == 4


def test_a_column_of_common_values_has_no_histogram_query() -> None:
    # 30 ordinary rows: each value with at least 1 row is common.
    fake, found = read(COLUMN, (31, 30, 30), [("1", 10), ("2", 20)])
    assert len(fake.queries) == 3
    assert fake.queries[-1][1] == {"least": 1}
    assert (found.null_rows, found.histogram_rows, found.bounds) == (1, 0, ())


def test_one_row_left_makes_a_histogram_of_one_bucket() -> None:
    fake, found = read(COLUMN, (20001, 20001, 20001), [("7", 20000)], (1, ["5", "5"]))
    assert fake.queries[-1][1] == {"fractions": [0.5, 0.5], "common": ["7"]}
    assert (found.histogram_rows, found.bounds) == (1, ("5", "5"))


def test_an_empty_table_has_no_histogram_query() -> None:
    fake, found = read(COLUMN, (0, 0, 0), [])
    assert len(fake.queries) == 3
    assert (found.rows, found.common, found.bounds) == (0, (), ())


def test_rows_deleted_after_they_were_counted_leave_no_histogram() -> None:
    _, found = read(COLUMN, COUNTS, COMMON, (0, None))
    assert (found.histogram_rows, found.bounds) == (0, ())
    assert found.distribution()["histogram"] is None


@pytest.mark.parametrize(
    ("type_name", "ordinary"),
    [
        ("numeric", "s.v IS NOT NULL AND s.v NOT IN ('NaN', 'Infinity', '-Infinity')"),
        ("date", "s.v IS NOT NULL AND pg_catalog.isfinite(s.v)"),
        ("timestamp", "s.v IS NOT NULL AND pg_catalog.isfinite(s.v)"),
        ("timestamptz", "s.v IS NOT NULL AND pg_catalog.isfinite(s.v)"),
        ("int2", "s.v IS NOT NULL"),
        ("int8", "s.v IS NOT NULL"),
    ],
)
def test_special_values_are_not_ordinary(type_name: str, ordinary: str) -> None:
    column = Column("s", "t", "c", "s.t", type_name, None, 1)
    fake, found = read(column, (20010, 20008, 20004), [], (20004, ["1", "2"]))
    # 2 NULL rows and 4 at special values.
    assert (found.rows, found.null_rows, found.special_rows) == (20010, 2, 4)
    counts, common, bounds = (query for query, _ in fake.queries[1:])
    assert f"count(*) FILTER (WHERE {ordinary}) FROM" in counts
    assert f"WHERE {ordinary} GROUP BY" in common
    assert (
        f'WHERE {ordinary} AND s.v <> ALL (%(common)s::text[]::"pg_catalog"."{type_name}"[])'
        in (bounds)
    )


def test_the_timeout_is_checked_before_any_query() -> None:
    fake = FakeConnection()
    with pytest.raises(ValueError, match="statement_timeout_ms must be positive, not 0"):
        read_quantiles(conn_of(fake), COLUMN, statement_timeout_ms=0)
    assert fake.queries == []


# The distribution and the plan


def test_the_distribution_is_the_input_of_section_1() -> None:
    _, found = read(COLUMN, COUNTS, COMMON, BOUNDS)
    assert found.distribution() == {
        "column": "id",
        "type": "int4",
        "null_rows": 5,
        "mcv": [{"value": "7", "rows": 20000}, {"value": "42", "rows": 5001}],
        "histogram": {"bounds": ["1", "5", "5", "9"], "rows": 4},
        "source": {
            "statistics": "exact",
            "table": QUALIFIED,
            "rows": 25010,
            "unestimated_rows": 0,
            "warnings": [],
        },
    }


def test_special_values_are_left_out_with_a_warning() -> None:
    column = Column("s", "t", "n", "s.t", "numeric", 2, 1)
    found = Quantiles(column, 9, 1, 6, (("1.50", 2),), 0, ())
    data = found.distribution()
    assert (data["scale"], data["null_rows"], data["histogram"]) == (2, 1, None)
    source: dict[str, Any] = data["source"]  # type: ignore[assignment]
    assert source["unestimated_rows"] == 6
    assert source["warnings"] == [
        {
            "code": "special_values_left_out",
            "rows": 6,
            "message": "6 rows at special values such as infinity are left out of the"
            " estimates; the predicates still read them",
        }
    ]


def test_a_value_that_is_not_in_its_text_form_is_an_error() -> None:
    found = Quantiles(COLUMN, 2, 0, 0, (("x", 2),), 0, ())
    with pytest.raises(SpecError) as raised:
        found.distribution()
    assert raised.value.code == "invalid_value"


def test_the_distribution_from_every_row() -> None:
    fake = FakeConnection(FIND_COLUMN_ROW, None, COUNTS, COMMON, BOUNDS)
    data = exact_distribution(conn_of(fake), QUALIFIED, "id")
    assert "partitions" not in data
    assert data["mcv"] == [{"value": "7", "rows": 20000}, {"value": "42", "rows": 5001}]
    # find_column reads the catalog first; the quantiles are rolled back.
    assert fake.rollbacks == [False, True]


def test_the_plan_from_every_row_is_exact_and_reports_the_planning_time() -> None:
    fake = FakeConnection(FIND_COLUMN_ROW, None, COUNTS, COMMON, BOUNDS)
    result = plan_from_exact(conn_of(fake), QUALIFIED, "id", 3)
    source: dict[str, Any] = result.source  # type: ignore[assignment]
    assert (source["statistics"], source["rows"]) == ("exact", 25010)
    assert isinstance(source["planning_ms"], float)
    assert source["planning_ms"] >= 0
    assert result.total_rows == 25010
    # 7 holds most of the rows and gets its own predicate.
    assert result.predicates[0] == '"id" IS NULL'
    assert [(part.sql, part.estimated_rows) for part in result.parts if part.kind == "value"] == [
        ('"id" = 7', 20000)
    ]


def test_the_planning_time_is_rounded_to_microseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([10.0, 10.0123456])
    monkeypatch.setattr(exact, "perf_counter", lambda: next(times))
    fake = FakeConnection(FIND_COLUMN_ROW, None, COUNTS, COMMON, BOUNDS)
    result = plan_from_exact(conn_of(fake), QUALIFIED, "id", 3)
    assert result.source["planning_ms"] == 12.346  # type: ignore[index]


@pytest.mark.parametrize("function", [exact_distribution, plan_from_exact])
def test_the_timeout_is_checked_before_connecting(function: Callable[..., object]) -> None:
    arguments = ("dsn", "t", "c") if function is exact_distribution else ("dsn", "t", "c", 4)
    with pytest.raises(ValueError, match="statement_timeout_ms must be positive, not -1"):
        function(*arguments, statement_timeout_ms=-1)


# Choosing the strategy


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    seen: list[tuple[object, ...]] = []
    for name in ("plan_from_exact", "exact_distribution"):
        monkeypatch.setattr(
            planning, name, lambda *args, _name=name, **kw: seen.append((_name, args, kw))
        )
    return seen


def test_the_exact_strategy_passes_the_timeout(calls: list[tuple[object, ...]]) -> None:
    planning.plan("dsn", "t", "c", 4, strategy="exact", statement_timeout_ms=9)
    planning.distribution("dsn", "t", "c", strategy="exact")
    assert calls == [
        ("plan_from_exact", ("dsn", "t", "c", 4), {"statement_timeout_ms": 9}),
        ("exact_distribution", ("dsn", "t", "c"), {"statement_timeout_ms": None}),
    ]


@pytest.mark.parametrize("option", ["sample_rows", "max_sample_rows", "seed"])
def test_sample_options_do_not_fit_the_exact_strategy(
    calls: list[tuple[object, ...]], option: str
) -> None:
    options: dict[str, Any] = {option: 5}
    with pytest.raises(ValueError, match="are for strategy='sample'"):
        planning.plan("dsn", "t", "c", 4, strategy="exact", **options)
    with pytest.raises(ValueError, match="are for strategy='sample'"):
        planning.distribution("dsn", "t", "c", strategy="exact", **options)
    assert calls == []


def test_the_module_reads_its_resolution_when_called(monkeypatch: pytest.MonkeyPatch) -> None:
    # The property tests shrink the resolution to reach the histogram with few rows.
    monkeypatch.setattr(exact, "RESOLUTION", 3)
    assert least_common_rows(10) == 4
    assert len(bound_fractions(100)) == 4
