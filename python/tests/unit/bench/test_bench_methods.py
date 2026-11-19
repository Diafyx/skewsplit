from __future__ import annotations

import re
import sqlite3
from contextlib import closing

import pytest

from bench_methods import (
    bounds_query,
    hash_mod,
    literal,
    quantile_query,
    quantile_ranges,
    quote,
)


def matches(predicates: list[str], values: list[int | None]) -> dict[int | None, int]:
    """How many predicates each value matches, run in SQLite.

    SQLite has no ``::`` casts, so integer literals lose theirs first.
    """
    counts = dict.fromkeys(values, 0)
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute('CREATE TABLE t ("c" INTEGER)')
        db.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
        for predicate in predicates:
            sql = re.sub(r"'(-?\d+)'::bigint", r"\1", predicate)
            for (value,) in db.execute(f"SELECT c FROM t WHERE {sql}"):  # noqa: S608 -- test SQL
                counts[value] += 1
    return counts


def test_quote_doubles_quotes() -> None:
    assert quote('a"b') == '"a""b"'


def test_literal_casts_and_escapes() -> None:
    assert literal("2026-01-01 00:00:00", "timestamp") == "'2026-01-01 00:00:00'::timestamp"
    assert literal("it's", "text") == "'it''s'::text"


def test_bounds_query() -> None:
    assert bounds_query("customer_id") == (
        'SELECT min("customer_id")::text AS lo, max("customer_id")::text AS hi FROM "bench_orders"'
    )


def test_quantile_query_asks_for_the_inner_quantiles() -> None:
    assert quantile_query("c", 4) == (
        'SELECT (percentile_disc(ARRAY[0.25, 0.5, 0.75]::float8[]) WITHIN GROUP (ORDER BY "c"))'
        '::text[] AS bounds FROM "bench_orders"'
    )


def test_hash_mod_has_a_null_predicate_and_one_per_bucket() -> None:
    bucket = '((hashtextextended("c"::text, 0) % 3) + 3) % 3'
    assert hash_mod("c", 3) == ['"c" IS NULL', f"{bucket} = 0", f"{bucket} = 1", f"{bucket} = 2"]


def test_quantile_ranges_text() -> None:
    assert quantile_ranges("c", "bigint", ["10", "20"]) == [
        '"c" IS NULL',
        "\"c\" < '10'::bigint",
        "\"c\" >= '10'::bigint AND \"c\" < '20'::bigint",
        "\"c\" >= '20'::bigint",
    ]


@pytest.mark.parametrize(
    ("bounds", "ranges"),
    [
        ([], 1),
        (["5"], 2),
        (["1", "1", "1", "7"], 3),
        (["-3", "0", "0", "4", "9", "9"], 5),
    ],
)
def test_every_value_matches_exactly_one_range(bounds: list[str], ranges: int) -> None:
    predicates = quantile_ranges("c", "bigint", bounds)
    assert len(predicates) == ranges + 1
    values: list[int | None] = [None, *range(-10, 15), -(2**63), 2**63 - 1]
    assert set(matches(predicates, values).values()) == {1}
