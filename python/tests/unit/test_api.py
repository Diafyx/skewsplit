from __future__ import annotations

import doctest

import pytest

import skewsplit
from skewsplit.core import planner

DISTRIBUTION: dict[str, object] = {
    "column": "customer_id",
    "type": "int4",
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
}


def test_the_names_of_the_api() -> None:
    assert skewsplit.__all__ == [
        "Bound",
        "Plan",
        "PlanWarning",
        "Predicate",
        "SpecError",
        "__version__",
        "plan",
    ]
    for name in skewsplit.__all__:
        assert getattr(skewsplit, name) is not None


def test_the_examples() -> None:
    results = doctest.testmod(skewsplit)
    assert results.failed == 0
    assert results.attempted > 0


@pytest.mark.parametrize("partitions", [1, 4, 16])
def test_plan_adds_the_partitions_to_the_distribution(partitions: int) -> None:
    result = skewsplit.plan(DISTRIBUTION, partitions)
    assert result == planner.plan({**DISTRIBUTION, "partitions": partitions})
    assert result.requested_partitions == partitions
    # The distribution is not changed.
    assert "partitions" not in DISTRIBUTION


def test_plan_takes_partitions_only_as_the_argument() -> None:
    with pytest.raises(skewsplit.SpecError, match="give partitions as the argument") as caught:
        skewsplit.plan({**DISTRIBUTION, "partitions": 4}, 4)
    assert caught.value.code == "invalid_input"


@pytest.mark.parametrize("partitions", [0, 10001])
def test_plan_checks_the_partitions(partitions: int) -> None:
    with pytest.raises(skewsplit.SpecError) as caught:
        skewsplit.plan(DISTRIBUTION, partitions)
    assert caught.value.code == "invalid_partitions"


def test_plan_checks_the_distribution() -> None:
    with pytest.raises(skewsplit.SpecError) as caught:
        skewsplit.plan({**DISTRIBUTION, "type": "text"}, 4)
    assert caught.value.code == "unsupported_type"
