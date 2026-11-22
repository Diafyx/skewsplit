"""Choosing the strategy (spec section 17) against a fake connection.

Each row of the section's table has a test here; tests/integration covers
each one against PostgreSQL.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pytest
from psycopg import sql
from psycopg.rows import tuple_row

from skewsplit.core.planner import plan
from skewsplit.postgres import auto, planning
from skewsplit.postgres.auto import Choice, auto_distribution, choose, plan_auto
from skewsplit.postgres.catalog import CatalogError, Column

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg
    from typing_extensions import Self

    from skewsplit.core.plan import Plan


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

    def fetchall(self) -> object:
        return self.result


class FakeConnection:
    """Answers each query with the next result."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.queries: list[tuple[str, dict[str, object] | None]] = []

    def cursor(self, *, row_factory: object) -> FakeCursor:
        assert row_factory is tuple_row
        return FakeCursor(self)

    @contextmanager
    def transaction(self, *, force_rollback: bool = False) -> Iterator[None]:  # noqa: ARG002
        yield


QUALIFIED = 'sales."Orders"'
COLUMN = Column("sales", "Orders", "id", QUALIFIED, "int4", None, 16384)
TREE = [("r", "2000", QUALIFIED)]
ROW = ("0.05", "{42,7}", "{0.25,0.03}", "{1,250,500,750,1000}", -0.5)


def facts(modified: int | None) -> tuple[object, ...]:
    return ("2026-11-20 08:00:00.5+00", 3600, modified)


def chosen(*results: object, column: Column = COLUMN) -> tuple[FakeConnection, Choice]:
    """Choose for ``column``; ``results`` answer the statistics queries after the settings."""
    fake = FakeConnection(None, *results)
    return fake, choose(cast("psycopg.Connection[Any]", fake), column)


# The table of spec section 17, row by row


def test_a_type_that_ranges_cannot_split_gets_hash_predicates() -> None:
    column = replace(COLUMN, type_name="uuid", splittable=False)
    fake = FakeConnection()
    choice = choose(cast("psycopg.Connection[Any]", fake), column)
    assert (choice.strategy, choice.reason, choice.distribution) == (
        "hash",
        "unsupported_type",
        None,
    )
    assert choice.message == (
        "ranges cannot split uuid, so hash predicates split it;"
        " each partition reads the whole table"
    )
    # Nothing is read: the statistics would not help.
    assert fake.queries == []


def test_a_table_never_analyzed_is_sampled() -> None:
    _, choice = chosen([("r", "-1", QUALIFIED)], facts(None), None)
    assert (choice.strategy, choice.reason, choice.distribution) == (
        "sample",
        "statistics_missing",
        None,
    )
    assert choice.message.startswith(
        f"no statistics to use: {QUALIFIED} has never been vacuumed or analyzed"
    )


def test_a_column_without_statistics_is_sampled() -> None:
    _, choice = chosen(TREE, facts(0), None)
    assert (choice.strategy, choice.reason) == ("sample", "statistics_missing")
    assert choice.message.startswith("no statistics to use: pg_stats has no statistics")


@pytest.mark.parametrize(
    ("modified", "reason"), [(201, "statistics_stale"), (200, "statistics_fresh")]
)
def test_more_than_a_tenth_changed_is_stale(modified: int, reason: str) -> None:
    # 201 x 10 > 2000, but 200 x 10 is not: the threshold of spec section 13.
    _, choice = chosen(TREE, facts(modified), ROW)
    assert choice.reason == reason
    assert choice.strategy == ("sample" if reason == "statistics_stale" else "statistics")


def test_the_stale_message_names_the_counts() -> None:
    _, choice = chosen(TREE, facts(300), ROW)
    assert choice.message == (
        "the statistics are out of date: 300 of about 2000 rows changed since the last ANALYZE"
    )
    assert choice.distribution is None


def test_changes_not_counted_for_a_tree_are_unchecked() -> None:
    tree = [("p", "0", QUALIFIED), ("r", "2000", "sales.part")]
    _, choice = chosen(tree, facts(5), ROW)
    assert (choice.strategy, choice.reason) == ("sample", "statistics_unchecked")
    assert choice.message == (
        "PostgreSQL does not count the rows changed since the statistics were gathered,"
        " so they may be out of date"
    )


def test_a_table_without_activity_statistics_is_unchecked() -> None:
    _, choice = chosen(TREE, None, ROW)
    assert choice.reason == "statistics_unchecked"


def test_fresh_statistics_are_used_and_read_once() -> None:
    fake, choice = chosen(TREE, facts(150), ROW)
    assert (choice.strategy, choice.reason) == ("statistics", "statistics_fresh")
    assert choice.message == (
        "the statistics are fresh: 150 rows changed since the last ANALYZE, a tenth or less"
    )
    data = cast("dict[str, Any]", choice.distribution)
    assert data["source"]["statistics"] == "pg_stats"
    assert data["null_rows"] == 100
    assert fake.results == []


def test_other_catalog_errors_are_raised() -> None:
    with pytest.raises(CatalogError) as raised:
        chosen([], facts(0), None)
    assert raised.value.code == "table_not_found"


def test_the_report_of_a_choice() -> None:
    choice = Choice("sample", "statistics_stale", "why")
    assert choice.report() == {"strategy": "sample", "reason": "statistics_stale", "message": "why"}


# Planning with the choice


FRESH = Choice("statistics", "statistics_fresh", "fresh", None)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Replace the catalog and the strategies, and record what they were asked."""
    seen: list[tuple[object, ...]] = []

    def find_column(_conn: object, table: str, column: str, *, any_type: bool = False) -> Column:
        seen.append(("find_column", table, column, any_type))
        return COLUMN

    def planned(name: str, rows: int) -> Any:  # noqa: ANN401 -- stands in for a strategy
        def strategy(*args: object, **kw: object) -> Plan:
            seen.append((name, args[1:], kw))
            data = {"column": "id", "type": "int4", "null_rows": 0, "mcv": [], "histogram": None}
            result = plan({**data, "partitions": 4, "source": {"statistics": name}})
            return replace(result, null_rows=rows)

        return strategy

    monkeypatch.setattr(auto, "find_column", find_column)
    monkeypatch.setattr(auto, "plan_from_sample", planned("sample", 1))
    monkeypatch.setattr(auto, "plan_from_hash", planned("hash", 2))

    def sample_distribution(*args: object, **kw: object) -> dict[str, object]:
        seen.append(("sample_distribution", args[1:], kw))
        return {"column": "id", "source": {"statistics": "sample"}}

    monkeypatch.setattr(auto, "sample_distribution", sample_distribution)
    return seen


def use(monkeypatch: pytest.MonkeyPatch, choice: Choice) -> None:
    monkeypatch.setattr(auto, "choose", lambda *_, **__: choice)


def conn() -> psycopg.Connection[Any]:
    return cast("psycopg.Connection[Any]", FakeConnection())


@pytest.mark.parametrize("hash_allowed", [True, False])
def test_the_plan_follows_the_choice_and_says_why(
    patched: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch, *, hash_allowed: bool
) -> None:
    use(monkeypatch, Choice("sample", "statistics_stale", "stale"))
    result = plan_auto(conn(), "t", "id", 4, hash_allowed=hash_allowed, seed=3, sample_rows=50)
    assert patched[0] == ("find_column", "t", "id", hash_allowed)
    assert patched[1] == (
        "sample",
        ("t", "id", 4),
        {
            "sample_rows": 50,
            "max_sample_rows": None,
            "seed": 3,
            "statement_timeout_ms": None,
            "where": None,
        },
    )
    source: dict[str, Any] = result.source  # type: ignore[assignment]
    assert source["statistics"] == "sample"
    assert source["auto"] == {
        "strategy": "sample",
        "reason": "statistics_stale",
        "message": "stale",
    }
    assert isinstance(source["planning_ms"], float)
    assert result.report.splitlines()[1] == "strategy sample, chosen by auto: stale"


def test_the_sample_size_defaults_as_for_the_sample_strategy(
    patched: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, Choice("sample", "statistics_missing", "missing"))
    plan_auto(conn(), "t", "id", 4)
    assert cast("dict[str, object]", patched[1][2])["sample_rows"] == 30000


def test_a_hash_choice_plans_hash_predicates(
    patched: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, Choice("hash", "unsupported_type", "hash it"))
    result = plan_auto(conn(), "t", "id", 4, statement_timeout_ms=7)
    assert patched[1] == ("hash", ("t", "id", 4), {"statement_timeout_ms": 7, "where": None})
    assert result.null_rows == 2


def test_fresh_statistics_are_planned_without_reading_them_again(
    patched: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = {
        "column": "id",
        "type": "int4",
        "null_rows": 5,
        "mcv": [],
        "histogram": {"bounds": ["1", "100"], "rows": 95},
        "source": {"statistics": "pg_stats"},
    }
    use(monkeypatch, replace(FRESH, distribution=data))
    result = plan_auto(conn(), "t", "id", 4)
    assert [call[0] for call in patched] == ["find_column"]
    assert (result.total_rows, result.null_rows) == (100, 5)
    assert result.source["auto"]["reason"] == "statistics_fresh"  # type: ignore[index]


def test_the_distribution_follows_the_choice(
    patched: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, Choice("sample", "statistics_missing", "missing"))
    data = auto_distribution(conn(), "t", "id", max_sample_rows=60000)
    # A distribution is for ranges, so the column must be one they can split.
    assert patched[0] == ("find_column", "t", "id", False)
    assert patched[1][0] == "sample_distribution"
    assert cast("dict[str, object]", patched[1][2])["max_sample_rows"] == 60000
    source: dict[str, Any] = data["source"]  # type: ignore[assignment]
    assert source == {
        "statistics": "sample",
        "auto": {"strategy": "sample", "reason": "statistics_missing", "message": "missing"},
    }


def test_a_fresh_distribution_is_the_statistics(
    patched: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    use(
        monkeypatch,
        replace(FRESH, distribution={"column": "id", "source": {"statistics": "pg_stats"}}),
    )
    data = auto_distribution(conn(), "t", "id")
    assert [call[0] for call in patched] == ["find_column"]
    assert data["source"]["auto"]["strategy"] == "statistics"  # type: ignore[index]


@pytest.mark.parametrize("function", [auto_distribution, plan_auto])
def test_the_timeout_is_checked_before_connecting(function: Any) -> None:  # noqa: ANN401
    arguments = ("dsn", "t", "c") if function is auto_distribution else ("dsn", "t", "c", 4)
    with pytest.raises(ValueError, match="statement_timeout_ms must be positive, not 0"):
        function(*arguments, statement_timeout_ms=0)


# Choosing auto in the API


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    seen: list[tuple[object, ...]] = []
    for name in ("plan_auto", "auto_distribution"):
        monkeypatch.setattr(
            planning, name, lambda *args, _name=name, **kw: seen.append((_name, args, kw))
        )
    return seen


def test_auto_may_plan_hash_predicates_unless_ranges_are_asked_for(
    calls: list[tuple[object, ...]],
) -> None:
    planning.plan("dsn", "t", "c", 4, strategy="auto", seed=1)
    planning.plan("dsn", "t", "c", 4, method="range", strategy="auto")
    planning.distribution("dsn", "t", "c", strategy="auto", sample_rows=9)
    options = {"max_sample_rows": None, "statement_timeout_ms": None, "where": None}
    assert calls == [
        (
            "plan_auto",
            ("dsn", "t", "c", 4),
            {"hash_allowed": True, "sample_rows": None, "seed": 1, **options},
        ),
        (
            "plan_auto",
            ("dsn", "t", "c", 4),
            {"hash_allowed": False, "sample_rows": None, "seed": None, **options},
        ),
        ("auto_distribution", ("dsn", "t", "c"), {"sample_rows": 9, "seed": None, **options}),
    ]


def test_auto_is_for_ranges_when_hash_is_asked_for(calls: list[tuple[object, ...]]) -> None:
    with pytest.raises(ValueError, match="are for method='range'"):
        planning.plan("dsn", "t", "c", 4, method="hash", strategy="auto")
    assert calls == []


@pytest.mark.parametrize("strategy", ["statistics", "exact"])
def test_sample_options_need_sample_or_auto(calls: list[tuple[object, ...]], strategy: str) -> None:
    message = "sample_rows, max_sample_rows and seed are for strategy='sample' or 'auto'"
    with pytest.raises(ValueError, match=message):
        planning.plan("dsn", "t", "c", 4, strategy=strategy, seed=1)  # type: ignore[arg-type]
    assert calls == []


def test_the_planning_time_is_rounded_to_microseconds(
    patched: list[tuple[object, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, Choice("sample", "statistics_missing", "missing"))
    times = iter([10.0, 10.0123456])
    monkeypatch.setattr(auto, "perf_counter", lambda: next(times))
    result = plan_auto(conn(), "t", "id", 4)
    assert result.source["planning_ms"] == 12.346  # type: ignore[index]
    assert len(patched) == 2
