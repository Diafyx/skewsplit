"""Reading pg_stats against a fake connection.

The queries themselves run against every supported PostgreSQL version in
tests/integration; these tests cover how their results are read.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from psycopg.rows import tuple_row

from skewsplit.core.errors import SpecError
from skewsplit.core.pgstats import from_pg_stats
from skewsplit.core.planner import plan
from skewsplit.postgres import statistics
from skewsplit.postgres.catalog import CatalogError, Column
from skewsplit.postgres.statistics import (
    Statistics,
    distribution,
    plan_from_statistics,
    read_statistics,
)

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

    def execute(self, query: str, params: dict[str, object] | None = None) -> FakeCursor:
        self.conn.queries.append((query, params))
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
TABLE = [("r", "2000", QUALIFIED)]
FACTS = ("2026-11-20 08:00:00.5+00", 3600, 150)
# The first example of spec section 13.
ROW = ("0.05", "{42,7}", "{0.25,0.03}", "{1,250,500,750,1000}", -0.5)


def read(*results: object, column: Column = COLUMN) -> tuple[FakeConnection, Statistics]:
    """Read statistics; ``results`` answer the tree, facts and pg_stats queries."""
    fake = FakeConnection(None, *results)
    return fake, read_statistics(cast("psycopg.Connection[Any]", fake), column)


def error_of(*results: object) -> CatalogError:
    with pytest.raises(CatalogError) as caught:
        read(*results)
    return caught.value


def test_the_tables_own_statistics() -> None:
    fake, stats = read(TABLE, FACTS, ROW)
    assert stats == Statistics(
        column=COLUMN,
        inherited=False,
        reltuples=("2000",),
        null_frac="0.05",
        most_common_vals="{42,7}",
        most_common_freqs="{0.25,0.03}",
        histogram_bounds="{1,250,500,750,1000}",
        n_distinct=-0.5,
        analyzed_at="2026-11-20 08:00:00.5+00",
        age_seconds=3600,
        modified_since_analyze=150,
    )
    settings, tree, facts, row = fake.queries
    assert "set_config('DateStyle', 'ISO', true)" in settings[0]
    assert "set_config('TimeZone', 'UTC', true)" in settings[0]
    assert "set_config('extra_float_digits', '1', true)" in settings[0]
    assert tree[1] == facts[1] == {"relid": 16384}
    assert row[1] == {"schema": "sales", "table": "Orders", "column": "id", "inherited": False}
    assert fake.rollbacks == [True]


def test_the_conversion_input_of_spec_section_13() -> None:
    _, stats = read(TABLE, FACTS, ROW)
    data = stats.conversion_input(4)
    assert data == {
        "column": "id",
        "type": "int4",
        "partitions": 4,
        "reltuples": ["2000"],
        "null_frac": "0.05",
        "most_common_vals": "{42,7}",
        "most_common_freqs": "{0.25,0.03}",
        "histogram_bounds": "{1,250,500,750,1000}",
        "modified_since_analyze": 150,
    }
    converted = from_pg_stats(data)
    assert converted["null_rows"] == 100
    assert converted["mcv"] == [{"value": "42", "rows": 500}, {"value": "7", "rows": 60}]


@pytest.mark.parametrize("scale", [0, 2])
def test_a_numeric_column_has_its_scale(scale: int) -> None:
    column = Column("sales", "Orders", "amount", QUALIFIED, "numeric", scale, 16384)
    _, stats = read(TABLE, FACTS, ("0", None, None, "{1,2}", -1.0), column=column)
    data = stats.conversion_input(2)
    assert list(data)[:3] == ["column", "type", "scale"]
    assert data["scale"] == scale


def test_a_tree_uses_the_inherited_statistics() -> None:
    tree = [
        ("p", "10000", QUALIFIED),
        ("r", "4000", "sales.part_1"),
        ("p", "6000", "sales.part_2"),
        ("f", "6000", "sales.part_2a"),
    ]
    fake, stats = read(tree, FACTS, ROW)
    assert fake.queries[3][1] == {
        "schema": "sales",
        "table": "Orders",
        "column": "id",
        "inherited": True,
    }
    # Partitioned tables store no rows, and changes since the tree was
    # analyzed are not counted.
    assert stats.inherited
    assert stats.reltuples == ("4000", "6000")
    assert stats.conversion_input(4)["reltuples"] == ["4000", "6000"]
    assert stats.modified_since_analyze is None
    assert stats.analyzed_at == FACTS[0]


def test_a_partitioned_table_without_a_row_count_is_fine() -> None:
    _, stats = read([("p", "-1", QUALIFIED), ("r", "5", "sales.part")], FACTS, ROW)
    assert stats.reltuples == ("5",)


def test_a_table_that_was_never_analyzed() -> None:
    error = error_of([("r", "-1", QUALIFIED)], FACTS, None)
    assert error.code == "statistics_missing"
    assert error.message == (
        'sales."Orders" has never been vacuumed or analyzed, so PostgreSQL has no row'
        " count for it; run ANALYZE on it"
    )


def test_children_that_were_never_analyzed() -> None:
    tree = [("r", "10", QUALIFIED), ("r", "-1", "sales.a"), ("r", "-1", "sales.b")]
    error = error_of(tree, FACTS, ROW)
    assert error.code == "statistics_missing"
    assert error.message.startswith("sales.a, sales.b have never been vacuumed or analyzed")
    assert error.message.endswith("run ANALYZE on them")


def test_a_table_with_rows_and_no_statistics() -> None:
    error = error_of(TABLE, FACTS, None)
    assert error.code == "statistics_missing"
    assert error.message == (
        'pg_stats has no statistics for column \'id\' of sales."Orders"; run ANALYZE sales."Orders"'
    )


def test_a_tree_without_inherited_statistics() -> None:
    error = error_of([("p", "-1", QUALIFIED), ("r", "3", "sales.part")], FACTS, None)
    assert error.code == "statistics_missing"
    assert error.message.startswith(
        "pg_stats has no statistics for column 'id' of sales.\"Orders\";"
        ' run ANALYZE sales."Orders". '
    )
    assert "autovacuum never gathers them" in error.message


@pytest.mark.parametrize(
    ("tree", "reltuples"),
    [
        ([("r", "0", QUALIFIED)], ("0",)),
        ([("p", "0", QUALIFIED), ("r", "0", "sales.a"), ("r", "0", "sales.b")], ("0", "0")),
        # A partitioned table with no partitions stores no rows at all.
        ([("p", "-1", QUALIFIED)], ("0",)),
    ],
)
def test_an_empty_table_has_no_statistics(
    tree: list[tuple[str, str, str]], reltuples: tuple[str, ...]
) -> None:
    _, stats = read(tree, FACTS, None)
    assert stats.reltuples == reltuples
    assert (stats.null_frac, stats.n_distinct) == ("0", None)
    assert stats.most_common_vals is stats.most_common_freqs is stats.histogram_bounds is None
    converted = from_pg_stats(stats.conversion_input(3))
    assert plan(converted).warnings[-1].code == "no_rows"


def test_postgres_may_not_know_when_the_table_was_analyzed() -> None:
    _, stats = read(TABLE, None, ROW)
    assert stats.analyzed_at is None
    assert stats.age_seconds is None
    assert stats.modified_since_analyze is None


def test_a_dropped_table() -> None:
    error = error_of([], None, None)
    assert (error.code, error.message) == ("table_not_found", 'sales."Orders" no longer exists')


def plan_with(monkeypatch: pytest.MonkeyPatch, partitions: int) -> tuple[FakeConnection, Any]:
    clock = iter([10.0, 10.0042])
    monkeypatch.setattr(statistics, "perf_counter", lambda: next(clock))
    fake = FakeConnection(FIND_COLUMN_ROW, None, TABLE, FACTS, ROW)
    conn = cast("psycopg.Connection[Any]", fake)
    return fake, plan_from_statistics(conn, QUALIFIED, "id", partitions)


def test_plan_from_statistics(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, result = plan_with(monkeypatch, 4)
    _, stats = read(TABLE, FACTS, ROW)
    expected = plan(from_pg_stats(stats.conversion_input(4)))
    assert result.parts == expected.parts
    assert result.source == {
        "statistics": "pg_stats",
        "rows_estimate": 2000,
        "unestimated_rows": 0,
        "modified_since_analyze": 150,
        "warnings": [],
        "table": QUALIFIED,
        "inherited": False,
        "analyzed_at": "2026-11-20 08:00:00.5+00",
        "statistics_age_seconds": 3600,
        "n_distinct": -0.5,
        "planning_ms": 4.2,
    }
    # find_column's lookup commits its savepoint; the statistics are read in
    # a transaction that is always rolled back.
    assert fake.rollbacks == [False, True]


def test_plan_from_statistics_checks_the_partitions(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SpecError) as caught:
        plan_with(monkeypatch, 0)
    assert caught.value.code == "invalid_partitions"


def test_the_distribution_is_the_input_without_partitions() -> None:
    fake = FakeConnection(FIND_COLUMN_ROW, None, TABLE, FACTS, ROW)
    found = distribution(cast("psycopg.Connection[Any]", fake), QUALIFIED, "id")
    _, stats = read(TABLE, FACTS, ROW)
    expected = from_pg_stats(stats.conversion_input(4))
    assert "partitions" not in found
    assert {**found, "partitions": 4, "source": None} == {**expected, "source": None}
    assert found["source"] == {
        **cast("dict[str, object]", expected["source"]),
        **stats.report(),
    }
    assert plan({**found, "partitions": 4}).parts == plan(expected).parts
