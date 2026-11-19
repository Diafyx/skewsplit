"""Micro-benchmarks of the planner.

A plain run executes each once, untimed (``--benchmark-disable`` in the
pytest options). ``make bench-core`` times them, and the core-bench CI job
reports the timings in its summary without gating on them.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from skewsplit.core.inputs import parse_input
from skewsplit.core.planner import plan

if TYPE_CHECKING:
    from pytest_benchmark.fixture import BenchmarkFixture

# The input of spec section 1.
SMALL: dict[str, Any] = {
    "column": "customer_id",
    "type": "int4",
    "partitions": 4,
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
}


def large(partitions: int, values: int) -> dict[str, Any]:
    """An int8 column with a histogram of ``values + 1`` bounds and ``values`` common values."""
    rng = random.Random(1)  # noqa: S311 -- reproducible test data, not security
    ordinals = sorted(rng.sample(range(-(10**15), 10**15), 2 * values + 1))
    return {
        "column": "id",
        "type": "int8",
        "partitions": partitions,
        "null_rows": 5,
        "mcv": [{"value": str(v), "rows": rng.randint(1, 50000)} for v in ordinals[1::2]],
        "histogram": {"bounds": [str(b) for b in ordinals[::2]], "rows": 10**9},
    }


def test_plan_small(benchmark: BenchmarkFixture) -> None:
    assert benchmark(plan, SMALL).planned_partitions == 4


def test_plan_1000_partitions(benchmark: BenchmarkFixture) -> None:
    data = large(1000, 1000)
    assert benchmark(plan, data).planned_partitions == 1000


def test_plan_at_the_input_limits(benchmark: BenchmarkFixture) -> None:
    data = large(10000, 10000)
    assert benchmark(plan, data).planned_partitions == 10000


def test_parse_input_at_the_input_limits(benchmark: BenchmarkFixture) -> None:
    data = large(10000, 10000)
    assert len(benchmark(parse_input, data).bounds) == 10001
