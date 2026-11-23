"""Invariants of every plan (spec sections 4 to 8), on random valid inputs."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from skewsplit.core.codecs import Codec, codec_for
from skewsplit.core.planner import plan

if TYPE_CHECKING:
    import random

    from skewsplit.core.plan import Plan, Predicate

pytestmark = pytest.mark.conformance

TYPES = ("int2", "int4", "int8", "numeric", "date", "timestamp", "timestamptz")
INTEGER_TYPES = ("int2", "int4", "int8")
# Names that need quoting, including a double quote and an SQL keyword.
COLUMNS = ("c", "customer_id", 'a"b', "Order Id", "select")
# The int8 minimum is the one integer written with a PostgreSQL cast (spec
# section 5). SQLite has no :: casts, so the test writes it as SQLite's CAST.
INT8_MINIMUM = "'-9223372036854775808'::int8"


def in_sqlite(predicate_sql: str) -> str:
    """The predicate in SQLite's dialect."""
    translated = predicate_sql.replace(INT8_MINIMUM, "CAST('-9223372036854775808' AS INTEGER)")
    assert "::" not in translated, predicate_sql
    return translated


@st.composite
def inputs(draw: st.DrawFn, types: tuple[str, ...] = TYPES) -> dict[str, Any]:
    """A valid input: values in a window of the type, which may reach its limits."""
    type_name = draw(st.sampled_from(types))
    scale = draw(st.integers(0, 3)) if type_name == "numeric" else None
    codec = codec_for(type_name, scale)
    width = min(
        draw(st.sampled_from([10, 1000, 10**9, 2**70])), codec.max_ordinal - codec.min_ordinal
    )
    start = draw(st.integers(codec.min_ordinal, codec.max_ordinal - width))
    if draw(st.booleans()):
        start = draw(st.sampled_from([codec.min_ordinal, codec.max_ordinal - width]))
    chosen = draw(st.sets(st.integers(start, start + width), max_size=12))
    if draw(st.booleans()):  # values at the type's limits, where x_hi can be MAX + 1
        chosen |= {codec.min_ordinal, codec.max_ordinal}
    pool = sorted(chosen)
    bounds = sorted(draw(st.lists(st.sampled_from(pool), min_size=2, max_size=10))) if pool else []
    if len(bounds) < 2 or draw(st.booleans()):
        bounds = []
    values = [v for v in pool if v not in bounds and draw(st.booleans())]
    # Some values far heavier than the rest, so that many become dominant.
    rows = st.one_of(st.integers(1, 100), st.integers(1, 10**12), st.integers(10**13, 10**15))
    data: dict[str, Any] = {"column": draw(st.sampled_from(COLUMNS)), "type": type_name}
    if scale is not None:
        data["scale"] = scale
    data.update(
        {
            "partitions": draw(st.one_of(st.integers(1, 12), st.integers(1, 200))),
            "null_rows": draw(st.one_of(st.just(0), st.integers(0, 10**6))),
            "mcv": [{"value": codec.text(v), "rows": draw(rows)} for v in values],
            "histogram": {
                "bounds": [codec.text(b) for b in bounds],
                "rows": draw(st.one_of(st.integers(0, 100), st.integers(0, 10**12))),
            }
            if bounds
            else None,
        }
    )
    return data


def matches(codec: Codec, predicate: Predicate, ordinal: int) -> bool:
    """Whether a non-NULL value matches a predicate, read from its structure."""
    if predicate.kind == "null":
        return False
    if predicate.kind == "value":
        assert predicate.value is not None
        return ordinal == codec.parse(predicate.value)
    low, high = predicate.lower, predicate.upper
    if low is not None:
        bound = codec.parse(low.value)
        if not (ordinal >= bound if low.op == ">=" else ordinal > bound):
            return False
    return high is None or ordinal < codec.parse(high.value)


def probes(codec: Codec, result: Plan, data: dict[str, Any], extra: list[int]) -> list[int]:
    """The type limits, every input value and boundary with its neighbours, and more."""
    points = {codec.min_ordinal, codec.max_ordinal, *extra}
    texts = [m["value"] for m in data["mcv"]] + (data["histogram"] or {"bounds": []})["bounds"]
    for predicate in result.parts:
        texts += [b.value for b in (predicate.lower, predicate.upper) if b is not None]
    for ordinal in map(codec.parse, texts):
        points.update({ordinal - 1, ordinal, ordinal + 1})
    return sorted(p for p in points if codec.min_ordinal <= p <= codec.max_ordinal)


@given(inputs(), st.lists(st.integers(-(2**63), 2**63 - 1), max_size=5))
def test_every_value_matches_exactly_one_predicate(data: dict[str, Any], extra: list[int]) -> None:
    result = plan(data)
    codec = codec_for(data["type"], data.get("scale"))
    assert [p.kind for p in result.parts].count("null") == 1
    assert result.parts[0].kind == "null"
    for ordinal in probes(codec, result, data, extra):
        hits = [p.sql for p in result.parts if matches(codec, p, ordinal)]
        assert len(hits) == 1, (codec.text(ordinal), hits)


@given(inputs(INTEGER_TYPES), st.lists(st.integers(-(2**63), 2**63 - 1), max_size=5))
def test_the_sql_reads_every_row_exactly_once(data: dict[str, Any], extra: list[int]) -> None:
    """Run the predicates' SQL text in SQLite, NULL rows included."""
    result = plan(data)
    codec = codec_for(data["type"], None)
    column = '"' + data["column"].replace('"', '""') + '"'
    rows = [*probes(codec, result, data, extra), None, None]
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute(f"CREATE TABLE t ({column} INTEGER)")
        db.executemany("INSERT INTO t VALUES (?)", [(row,) for row in rows])
        hits: list[int] = []
        for predicate in result.parts:
            query = f"SELECT rowid FROM t WHERE {in_sqlite(predicate.sql)}"  # noqa: S608 -- the plan's own SQL
            hits += [rowid for (rowid,) in db.execute(query)]
    assert sorted(hits) == list(range(1, len(rows) + 1))


@given(inputs())
def test_the_plan_is_consistent(data: dict[str, Any]) -> None:
    result = plan(data)
    n = data["partitions"]
    codes = [w.code for w in result.warnings]
    estimates = [p.estimated_rows for p in result.parts]
    # Partitions: never more than N, and fewer only with a warning that says so.
    assert 1 <= result.planned_partitions <= n
    assert ("fewer_partitions" in codes) == (result.planned_partitions < n)
    # Rows: the estimates add up, and every dominant value has its own predicate.
    assert min(estimates) >= 0
    assert sum(estimates) == result.total_rows
    values = [p.value for p in result.parts if p.kind == "value"]
    assert values == [w.value for w in result.warnings if w.code == "dominant_value"]
    assert len(values) <= (n - 1) // 2
    # 4.3: in a segment with more than one range, every range holds a row.
    segments: list[list[int]] = [[]]
    for predicate in result.parts[1:]:
        if predicate.kind == "value":
            segments.append([])
        else:
            segments[-1].append(predicate.estimated_rows)
    assert all(min(rows) >= 1 for rows in segments if len(rows) > 1)
    # The skew score follows from the largest predicate.
    if result.total_rows:
        assert result.skew_milli == (
            (2 * max(estimates) * n * 1000 + result.total_rows) // (2 * result.total_rows)
        )
    else:
        assert (result.skew_milli, codes[-1]) == (None, "no_rows")


@given(inputs(), st.randoms(use_true_random=False))
def test_the_order_of_common_values_does_not_matter(
    data: dict[str, Any], rng: random.Random
) -> None:
    shuffled = {**data, "mcv": rng.sample(data["mcv"], len(data["mcv"]))}
    assert plan(shuffled).to_dict() == plan(data).to_dict()
