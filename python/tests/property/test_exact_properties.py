"""Fuzz the exact quantiles of spec section 15 against an emulation of their queries.

The emulation counts the values and picks each bound the way
``percentile_disc`` does, from the fractions the adapter sends, so the
tests check both that the fractions find the positions of the spec and its
accuracy claim. The queries run against PostgreSQL in tests/integration.
"""

from __future__ import annotations

from collections import Counter
from math import ceil
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from skewsplit.core.planner import plan
from skewsplit.postgres import exact
from skewsplit.postgres.catalog import Column
from skewsplit.postgres.exact import Quantiles, bound_fractions, least_common_rows

if TYPE_CHECKING:
    from skewsplit.core.plan import Predicate

pytestmark = pytest.mark.conformance

COLUMN = Column("public", "t", "c", "public.t", "int8", None, 1)


def emulate(rows: list[int | None]) -> Quantiles:
    """The three queries of section 15, run in Python on ``rows``."""
    ordinary = sorted(value for value in rows if value is not None)
    counts = Counter(ordinary)
    least = least_common_rows(len(ordinary))
    common = tuple((str(value), c) for value, c in sorted(counts.items()) if c >= least)
    rest = [value for value in ordinary if counts[value] < least]
    bounds: tuple[str, ...] = ()
    if rest:
        h = len(rest)
        # percentile_disc(f) is row max(1, ceil(f * h)), counting from 1.
        bounds = tuple(str(rest[max(1, ceil(f * h)) - 1]) for f in bound_fractions(h))
        buckets = len(bounds) - 1
        assert list(bounds) == [str(rest[j * (h - 1) // buckets]) for j in range(buckets + 1)]
    return Quantiles(COLUMN, len(rows), rows.count(None), 0, common, len(rest), bounds)


@given(
    st.lists(
        st.one_of(st.none(), st.integers(-30, 30), st.integers(-(10**4), 10**4)), max_size=3000
    ),
    st.integers(1, 64),
    # A small resolution reaches the histogram with few rows; the real one has none here.
    st.one_of(st.integers(1, 50), st.just(exact.RESOLUTION)),
)
def test_a_plan_from_every_row_is_within_two_buckets_of_the_rows(
    rows: list[int | None], partitions: int, resolution: int
) -> None:
    # Spec section 15: the NULL and value predicates are exact, and each range
    # reads within 2 x B rows of its estimate.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(exact, "RESOLUTION", resolution)
        data = emulate(rows).distribution()
    mcv: Any = data["mcv"]
    assert len(mcv) <= resolution
    histogram: Any = data["histogram"]
    bucket = 0
    if histogram is not None:
        assert len(histogram["bounds"]) <= resolution + 1
        bucket = ceil(histogram["rows"] / (len(histogram["bounds"]) - 1))
    elif resolution == exact.RESOLUTION:
        # Fewer than 10000 ordinary rows: every value is common.
        assert sum(entry["rows"] for entry in mcv) == len(rows) - rows.count(None)
    result = plan({**data, "partitions": partitions})
    assert result.total_rows == len(rows)
    for part in result.parts:
        actual = sum(1 for value in rows if _matches(part, value))
        if part.kind == "range":
            assert abs(actual - part.estimated_rows) <= 2 * bucket, part.sql
        else:
            assert actual == part.estimated_rows, part.sql


@given(st.integers(1, 2**51 - 1), st.data())
def test_the_fractions_find_their_positions_below_2_to_the_51(
    rows: int, data: st.DataObject
) -> None:
    buckets = min(exact.RESOLUTION, max(1, rows - 1))
    j = data.draw(st.integers(0, buckets))
    position = j * (rows - 1) // buckets
    fraction = (position + 0.5) / rows
    assert max(1, ceil(fraction * rows)) - 1 == position


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
