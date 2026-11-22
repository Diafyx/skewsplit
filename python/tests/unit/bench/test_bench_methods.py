from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from bench_methods import (
    METHODS,
    STRATEGIES,
    bounds_query,
    hash_mod,
    plan_path,
    quote,
    read_plan,
    strategy_of,
)
from skewsplit.core.planner import plan

if TYPE_CHECKING:
    from pathlib import Path


def pg_mod(sql: str) -> str:
    """PostgreSQL's hash-mod predicate, with a stand-in hash SQLite knows."""
    return re.sub(r"hashtextextended\(\"c\"::text, 0\)", '("c" * 7919 - 50000)', sql)


def matches(predicates: list[str], values: list[int | None]) -> dict[int | None, int]:
    """How many predicates each value matches, run in SQLite."""
    counts = dict.fromkeys(values, 0)
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute('CREATE TABLE t ("c" INTEGER)')
        db.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
        for predicate in predicates:
            for (value,) in db.execute(f"SELECT c FROM t WHERE {pg_mod(predicate)}"):  # noqa: S608 -- test SQL
                counts[value] += 1
    return counts


def test_the_methods_are_the_baselines_and_skewsplit_strategies() -> None:
    assert METHODS == (
        "default bounds",
        "hash-mod",
        "skewsplit statistics",
        "skewsplit sample",
    )
    assert STRATEGIES == ("statistics", "sample")


@pytest.mark.parametrize(
    ("method", "strategy"),
    [
        ("default bounds", None),
        ("hash-mod", None),
        ("skewsplit statistics", "statistics"),
        ("skewsplit sample", "sample"),
    ],
)
def test_strategy_of(method: str, strategy: str | None) -> None:
    assert strategy_of(method) == strategy


def test_quote_doubles_quotes() -> None:
    assert quote('a"b') == '"a""b"'


def test_bounds_query() -> None:
    assert bounds_query("customer_id") == (
        'SELECT min("customer_id")::text AS lo, max("customer_id")::text AS hi FROM "bench_orders"'
    )


def test_hash_mod_has_a_null_predicate_and_one_per_bucket() -> None:
    bucket = '((hashtextextended("c"::text, 0) % 3) + 3) % 3'
    assert hash_mod("c", 3) == ['"c" IS NULL', f"{bucket} = 0", f"{bucket} = 1", f"{bucket} = 2"]


def test_every_value_matches_exactly_one_hash_bucket() -> None:
    # SQLite's % truncates toward zero, as PostgreSQL's does.
    values: list[int | None] = [None, *range(-30, 30)]
    assert set(matches(hash_mod("c", 4), values).values()) == {1}


def test_plan_path(tmp_path: Path) -> None:
    assert plan_path(tmp_path, "created_at", "sample") == tmp_path / "created_at-sample.json"


def test_read_plan_takes_the_predicates_and_skewsplits_planning_time(tmp_path: Path) -> None:
    data = {
        "column": "c",
        "type": "int8",
        "partitions": 2,
        "null_rows": 1,
        "mcv": [],
        "histogram": {"bounds": ["1", "9"], "rows": 8},
        "source": {"statistics": "pg_stats", "planning_ms": 1234.5},
    }
    result = plan(data)
    path = tmp_path / "c-statistics.json"
    path.write_text(result.to_json(), encoding="utf-8")
    predicates, planning = read_plan(path)
    assert predicates == result.predicates
    assert planning == pytest.approx(1.2345)
    assert json.loads(path.read_text(encoding="utf-8"))["predicates"][0]["sql"] == '"c" IS NULL'
