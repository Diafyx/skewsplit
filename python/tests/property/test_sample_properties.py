"""Fuzz the sample conversion of spec section 14.

The reference conversion here writes out the list of section 14 value by
value and rounds with fractions, so it shares no code with the conversion.
"""

from __future__ import annotations

from collections import Counter
from fractions import Fraction
from math import ceil, floor
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from skewsplit.core.planner import plan
from skewsplit.core.sample import from_sample

if TYPE_CHECKING:
    from skewsplit.core.plan import Predicate

pytestmark = pytest.mark.conformance

SPECIAL = ("NaN", "Infinity", "-Infinity")


def scaled(count: int, total: int, size: int) -> int:
    return floor(Fraction(count * total, size) + Fraction(1, 2)) if size else 0


def reference(
    counts: dict[int, int], nulls: int, specials: dict[str, int], total: int
) -> tuple[int, list[dict[str, Any]], dict[str, Any] | None, int]:
    """Section 14, step by step: the input it builds from a numeric (scale 0) sample."""
    size = nulls + sum(counts.values()) + sum(specials.values())
    ordinary = sum(counts.values())
    common = {v: c for v, c in counts.items() if c >= 2 and c * 1000 >= ordinary}
    listed = [v for v in sorted(counts) if v not in common for _ in range(counts[v])]
    null_rows = scaled(nulls, total, size)
    unestimated = scaled(sum(specials.values()), total, size)
    mcv: list[dict[str, Any]] = [
        {"value": str(v), "rows": max(1, scaled(common[v], total, size))} for v in sorted(common)
    ]
    histogram: dict[str, Any] | None = None
    if listed:
        h = len(listed)
        m = min(1000, max(1, h - 1))
        rows = max(0, total - null_rows - sum(entry["rows"] for entry in mcv) - unestimated)
        histogram = {"bounds": [str(listed[j * (h - 1) // m]) for j in range(m + 1)], "rows": rows}
    return null_rows, mcv, histogram, unestimated


def conversion_input(total: int, nulls: int, entries: list[Any]) -> dict[str, Any]:
    return {
        "column": "c",
        "type": "numeric",
        "scale": 0,
        "partitions": 4,
        "rows_estimate": total,
        "sample_null_rows": nulls,
        "sample_values": entries,
    }


@st.composite
def samples(draw: st.DrawFn) -> tuple[dict[int, int], int, dict[str, int], int, list[Any]]:
    # A few heavy values among many light ones, so every rule of section 14 is reached.
    light = draw(st.dictionaries(st.integers(-(10**6), 10**6), st.integers(1, 3), max_size=1500))
    heavy = draw(st.dictionaries(st.integers(-50, 50), st.integers(1, 5000), max_size=6))
    counts = {**light, **heavy}
    specials = draw(st.dictionaries(st.sampled_from(SPECIAL), st.integers(1, 100)))
    nulls = draw(st.integers(0, 5000))
    size = nulls + sum(counts.values()) + sum(specials.values())
    total = draw(st.one_of(st.just(size), st.integers(0, 10**12)))
    pairs = [(str(v), c) for v, c in counts.items()] + list(specials.items())
    entries = [{"value": value, "rows": rows} for value, rows in draw(st.permutations(pairs))]
    return counts, nulls, specials, total, entries


@given(samples())
def test_the_conversion_follows_section_14_step_by_step(
    drawn: tuple[dict[int, int], int, dict[str, int], int, list[Any]],
) -> None:
    counts, nulls, specials, total, entries = drawn
    result = from_sample(conversion_input(total, nulls, entries))
    null_rows, mcv, histogram, unestimated = reference(counts, nulls, specials, total)
    assert (result["null_rows"], result["mcv"], result["histogram"]) == (null_rows, mcv, histogram)
    source: dict[str, Any] = result["source"]  # type: ignore[assignment]
    assert source["unestimated_rows"] == unestimated
    assert source["sample_rows"] == nulls + sum(counts.values()) + sum(specials.values())
    assert len(result["mcv"]) <= 1000  # type: ignore[arg-type]
    if histogram is not None:
        assert len(histogram["bounds"]) <= 1001


@given(
    st.lists(
        st.one_of(st.none(), st.integers(-30, 30), st.integers(-(10**4), 10**4)), max_size=4000
    ),
    st.integers(1, 64),
)
def test_a_plan_from_every_row_is_within_two_buckets_of_the_rows(
    rows: list[int | None], partitions: int
) -> None:
    # Spec section 14: when every row was read, the NULL and value predicates
    # are exact and each range reads within 2 x B rows of its estimate.
    counts = Counter(value for value in rows if value is not None)
    data = {
        "column": "c",
        "type": "int8",
        "partitions": partitions,
        "rows_estimate": len(rows),
        "sample_null_rows": rows.count(None),
        "sample_values": [{"value": str(v), "rows": c} for v, c in counts.items()],
    }
    converted = from_sample(data)
    histogram: Any = converted["histogram"]
    bucket = 0
    if histogram is not None:
        bucket = ceil(histogram["rows"] / (len(histogram["bounds"]) - 1))
    result = plan(converted)
    assert result.total_rows == len(rows)
    for part in result.parts:
        actual = sum(1 for value in rows if _matches(part, value))
        if part.kind == "range":
            assert abs(actual - part.estimated_rows) <= 2 * bucket, part.sql
        else:
            assert actual == part.estimated_rows, part.sql


def _matches(part: Predicate, value: int | None) -> bool:
    if part.kind == "null":
        return value is None
    if value is None:
        return False
    if part.kind == "value":
        return value == int(str(part.value))
    low, high = part.lower, part.upper
    above = low is None or (value >= int(low.value) if low.op == ">=" else value > int(low.value))
    return above and (high is None or value < int(high.value))
