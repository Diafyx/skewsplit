"""Fuzz the hash predicates of spec section 16.

PostgreSQL's ``%`` truncates toward zero, as in C, so the remainder of a
negative hash is negative. These tests emulate it to check that the
predicates' expression gives every hash exactly one remainder, and that the
estimates add up.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from skewsplit.core.hashing import hash_plan

pytestmark = pytest.mark.conformance

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


def pg_mod(dividend: int, divisor: int) -> int:
    """PostgreSQL's ``%`` on bigint: the sign of the dividend, as in C."""
    remainder = abs(dividend) % divisor
    return -remainder if dividend < 0 else remainder


@given(st.integers(INT64_MIN, INT64_MAX), st.integers(1, 10000))
def test_every_hash_has_exactly_one_remainder(h: int, n: int) -> None:
    inner = pg_mod(h, n)
    assert -n < inner < n
    # No step leaves bigint, so the expression never overflows.
    assert INT64_MIN <= inner + n <= INT64_MAX
    remainder = pg_mod(inner + n, n)
    assert 0 <= remainder < n
    assert remainder == h % n
    result = hash_plan(
        {"column": "c", "type": "text", "partitions": n, "null_rows": 0, "non_null_rows": 1}
    )
    matching = [part for part in result.parts[1:] if part.sql.endswith(f" = {remainder}")]
    assert [part.remainder for part in matching] == [remainder]


@given(st.integers(1, 10000), st.integers(0, 2**63 - 1), st.data())
def test_the_estimates_add_up_and_differ_by_at_most_one(
    n: int, total: int, data: st.DataObject
) -> None:
    null_rows = data.draw(st.integers(0, total))
    result = hash_plan(
        {
            "column": "c",
            "type": "uuid",
            "partitions": n,
            "null_rows": null_rows,
            "non_null_rows": total - null_rows,
        }
    )
    estimates = [part.estimated_rows for part in result.parts[1:]]
    assert len(estimates) == n == result.planned_partitions
    assert sum(estimates) == total - null_rows
    assert max(estimates) - min(estimates) <= 1
    assert [part.remainder for part in result.parts[1:]] == list(range(n))
    assert result.warnings[0].code == "full_scans"
