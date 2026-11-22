"""Converting a sample into a planner input (spec section 14).

The fixtures in spec/fixtures/sample/ check the contract; these tests pin each
rule at its edges.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from skewsplit.core.errors import SpecError
from skewsplit.core.sample import RESOLUTION, from_sample

INT64_MAX = 2**63 - 1


def sample(*pairs: tuple[str, int], **fields: object) -> dict[str, Any]:
    """A conversion input with these sampled values, T = n unless given."""
    data: dict[str, Any] = {
        "column": "c",
        "type": "int4",
        "partitions": 4,
        "rows_estimate": None,
        "sample_null_rows": 0,
        "sample_values": [{"value": value, "rows": rows} for value, rows in pairs],
    }
    data.update(fields)
    if data["rows_estimate"] is None:
        data["rows_estimate"] = data["sample_null_rows"] + sum(rows for _, rows in pairs)
    return data


def singles(start: int, count: int) -> list[tuple[str, int]]:
    return [(str(value), 1) for value in range(start, start + count)]


def common_values(data: dict[str, Any]) -> list[str]:
    mcv: Any = from_sample(data)["mcv"]
    return [entry["value"] for entry in mcv]


def histogram(data: dict[str, Any]) -> dict[str, Any] | None:
    found: dict[str, Any] | None = from_sample(data)["histogram"]  # type: ignore[assignment]
    return found


def error(data: object) -> SpecError:
    with pytest.raises(SpecError) as caught:
        from_sample(data)
    return caught.value


# Common values


def test_a_value_sampled_once_is_never_common() -> None:
    assert common_values(sample(("1", 1), ("2", 1))) == []


def test_a_value_sampled_twice_is_common_in_a_small_sample() -> None:
    assert common_values(sample(("1", 2), ("2", 1))) == ["1"]


@pytest.mark.parametrize(("ordinary", "common"), [(2000, ["0"]), (2001, [])])
def test_a_common_value_holds_a_thousandth_of_the_ordinary_rows(
    ordinary: int, common: list[str]
) -> None:
    # The value 0 has 2 of the rows: common exactly when 2 x 1000 >= S.
    data = sample(("0", 2), *singles(1, ordinary - 2), sample_null_rows=5000)
    assert common_values(data) == common


def test_special_and_null_rows_do_not_count_towards_the_threshold() -> None:
    # S = 2000 with NaN and NULL rows on top, so 0 with 2 rows is still common.
    data = sample(
        ("0", 2), *singles(1, 1998), ("NaN", 500), type="numeric", scale=0, sample_null_rows=9
    )
    assert common_values(data) == ["0"]


def test_common_values_are_in_order_of_ordinal_with_scaled_rows() -> None:
    data = sample(("10", 3), ("-5", 2), ("2", 4), rows_estimate=90)
    assert from_sample(data)["mcv"] == [
        {"value": "-5", "rows": 20},
        {"value": "2", "rows": 40},
        {"value": "10", "rows": 30},
    ]


def test_a_common_value_keeps_at_least_one_row() -> None:
    data = sample(("4", 2), sample_null_rows=10**6, rows_estimate=1)
    assert from_sample(data)["mcv"] == [{"value": "4", "rows": 1}]


# Scaling


@pytest.mark.parametrize(
    ("total", "nulls", "size", "expected"),
    [
        (5, 1, 2, 3),  # 2.5 rounds up
        (7, 1, 4, 2),  # 1.75 rounds up
        (5, 1, 4, 1),  # 1.25 rounds down
        (1000, 2, 15, 133),  # the example of spec section 14
        (INT64_MAX, 3, 3, INT64_MAX),
        (0, 3, 3, 0),
    ],
)
def test_counts_scale_to_the_nearest_row_half_up(
    total: int, nulls: int, size: int, expected: int
) -> None:
    data = sample(*singles(1, size - nulls), sample_null_rows=nulls, rows_estimate=total)
    assert from_sample(data)["null_rows"] == expected


def test_an_empty_sample_has_no_rows_whatever_the_estimate() -> None:
    result = from_sample(sample(rows_estimate=10**6))
    assert (result["null_rows"], result["mcv"], result["histogram"]) == (0, [], None)
    assert result["source"] == {
        "statistics": "sample",
        "rows_estimate": 10**6,
        "sample_rows": 0,
        "unestimated_rows": 0,
        "warnings": [],
    }


def test_when_every_row_was_read_the_counts_are_exact() -> None:
    data = sample(("7", 5), ("9", 1), ("8", 1), sample_null_rows=3)
    result = from_sample(data)
    assert result["null_rows"] == 3
    assert result["mcv"] == [{"value": "7", "rows": 5}]
    assert result["histogram"] == {"bounds": ["8", "9"], "rows": 2}


# The histogram


def test_without_ordinary_values_left_there_is_no_histogram() -> None:
    assert histogram(sample(("1", 2), ("2", 3), sample_null_rows=4)) is None


@pytest.mark.parametrize(
    ("values", "bounds"),
    [
        ([("5", 1)], ["5", "5"]),
        ([("5", 1), ("6", 1)], ["5", "6"]),
        ([("5", 1), ("6", 1), ("7", 1)], ["5", "6", "7"]),
    ],
)
def test_a_short_list_gives_one_bound_per_value(
    values: list[tuple[str, int]], bounds: list[str]
) -> None:
    found = histogram(sample(*values))
    assert found is not None
    assert found["bounds"] == bounds


def test_a_value_sampled_more_than_once_but_not_common_repeats_in_the_list() -> None:
    # S = 10000 and 9 x 1000 < S, so 0 is not common. -100 is, which leaves the
    # list 0 (9 times), 1, ..., 2991: h = 3000, and bound j is at j x 2999 / 1000.
    found = histogram(sample(("-100", 7000), ("0", 9), *singles(1, 2991)))
    assert found is not None
    assert found["bounds"][:5] == ["0", "0", "0", "0", "3"]


@pytest.mark.parametrize("count", [RESOLUTION, RESOLUTION + 1, RESOLUTION + 2, 3 * RESOLUTION])
def test_the_histogram_has_at_most_a_thousand_buckets(count: int) -> None:
    found = histogram(sample(*singles(0, count)))
    assert found is not None
    bounds = [int(bound) for bound in found["bounds"]]
    buckets = min(RESOLUTION, count - 1)
    assert len(bounds) == buckets + 1
    # Bound j is the value at position j x (h - 1) / m, and the values are 0 to h - 1.
    assert bounds == [j * (count - 1) // buckets for j in range(buckets + 1)]


def test_the_histogram_holds_the_rows_the_other_parts_leave() -> None:
    data = sample(("1", 3), ("8", 1), ("9", 1), ("NaN", 1), type="numeric", scale=0)
    data.update(sample_null_rows=1, rows_estimate=70)
    result = from_sample(data)
    # n = 7, so a sampled row stands for 10: NULL 10, 1 has 30, NaN 10 left out.
    assert result["histogram"] == {"bounds": ["8", "9"], "rows": 70 - 10 - 30 - 10}


def test_the_histogram_rows_are_never_negative() -> None:
    # T = 1: the common values keep 1 row each, which leaves less than nothing.
    found = histogram(sample(("1", 2), ("2", 2), ("9", 1), rows_estimate=1))
    assert found == {"bounds": ["9", "9"], "rows": 0}


def test_huge_counts_are_exact() -> None:
    # 1001 values of 2^50 rows each: none is common, and the last bound is at
    # position 1001 x 2^50 - 1, which a float would round up past the end.
    found = histogram(sample(*((str(value), 2**50) for value in range(1001))))
    assert found is not None
    assert found["bounds"][-2:] == ["999", "1000"]
    assert found["rows"] == 1001 * 2**50


def test_the_order_of_the_sample_does_not_matter() -> None:
    pairs = [("0", 7), *singles(1, 3000), ("-4", 1), ("5000", 9)]
    expected = from_sample(sample(*pairs, rows_estimate=10**7))
    shuffled = pairs[:]
    random.Random(25).shuffle(shuffled)  # noqa: S311 -- a fixed order, not security
    assert from_sample(sample(*shuffled, rows_estimate=10**7)) == expected


# Special values and the result


def test_special_values_are_left_out_with_a_warning() -> None:
    data = sample(("NaN", 2), ("-Infinity", 1), ("1.5", 1), type="numeric", scale=1)
    data.update(rows_estimate=40)
    result = from_sample(data)
    assert result["histogram"] == {"bounds": ["1.5", "1.5"], "rows": 10}
    source: dict[str, Any] = result["source"]  # type: ignore[assignment]
    assert source["unestimated_rows"] == 30
    [warning] = source["warnings"]
    assert {key: warning[key] for key in ("code", "rows")} == {
        "code": "special_values_left_out",
        "rows": 30,
    }
    assert warning["message"] == (
        "about 30 rows at special values such as infinity are left out of the estimates;"
        " the predicates still read them"
    )


def test_a_special_value_scaled_to_no_rows_gives_no_warning() -> None:
    data = sample(("infinity", 1), type="date", rows_estimate=0)
    source: dict[str, Any] = from_sample(data)["source"]  # type: ignore[assignment]
    assert (source["unestimated_rows"], source["warnings"]) == (0, [])


def test_the_result_is_a_planner_input_in_the_order_of_section_1() -> None:
    data = sample(("1.5", 1), type="numeric", scale=2, column="amount", partitions=9)
    result = from_sample(data)
    assert list(result) == [
        "column",
        "type",
        "scale",
        "partitions",
        "null_rows",
        "mcv",
        "histogram",
        "source",
    ]
    assert (result["column"], result["type"], result["scale"], result["partitions"]) == (
        "amount",
        "numeric",
        2,
        9,
    )
    assert result["histogram"] == {"bounds": ["1.50", "1.50"], "rows": 1}
    assert "scale" not in from_sample(sample(("1", 1)))


# Errors


ENTRIES = "sample_values must be a list of objects with a string value and an integer rows"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "the sample must be a JSON object"),
        (
            {},
            (
                "the sample is missing column, type, partitions, rows_estimate,"
                " sample_null_rows, sample_values"
            ),
        ),
        (sample(extra=1, more=2), "the sample has unknown fields: extra, more"),
        (sample(type=4), "type must be a JSON string"),
        (sample(scale="2"), "scale must be a JSON integer"),
        (sample(rows_estimate=True), "rows_estimate must be a JSON integer"),
        (sample(rows_estimate=1.0), "rows_estimate must be a JSON integer"),
        (sample(sample_null_rows="1", rows_estimate=1), "sample_null_rows must be a JSON integer"),
        (sample(sample_values={}), ENTRIES),
        (sample(sample_values=["1"]), ENTRIES),
        (sample(sample_values=[{"value": "1"}]), ENTRIES),
        (sample(sample_values=[{"value": "1", "rows": 1, "x": 0}]), ENTRIES),
        (sample(sample_values=[{"value": 1, "rows": 1}]), ENTRIES),
        (sample(sample_values=[{"value": "1", "rows": True}]), ENTRIES),
        (sample(sample_values=[{"value": "1", "rows": 1.0}]), ENTRIES),
    ],
)
def test_a_malformed_sample_is_invalid_input(data: object, message: str) -> None:
    found = error(data)
    assert (found.code, found.message) == ("invalid_input", message)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (sample(rows_estimate=-1), "rows_estimate and sample_null_rows must not be negative"),
        (sample(sample_null_rows=-1), "rows_estimate and sample_null_rows must not be negative"),
        (sample(("1", 0)), "every sampled value needs at least 1 row"),
        (sample(("1", -1), ("2", 2)), "every sampled value needs at least 1 row"),
        (sample(rows_estimate=INT64_MAX + 1), "rows_estimate and the sample size must be at"),
        (
            sample(("1", INT64_MAX), sample_null_rows=1, rows_estimate=1),
            "rows_estimate and the sample size must be at",
        ),
    ],
)
def test_counts_out_of_range_are_invalid_rows(data: object, message: str) -> None:
    found = error(data)
    assert (found.code, found.message[: len(message)]) == ("invalid_rows", message)


def test_the_largest_sample_is_valid() -> None:
    data = sample(("1", INT64_MAX - 1), sample_null_rows=1, rows_estimate=1)
    assert from_sample(data)["mcv"] == [{"value": "1", "rows": 1}]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            sample(("1.5", 1), ("1.50", 2), type="numeric", scale=2),
            "1.50 appears more than once in sample_values",
        ),
        (
            sample(("infinity", 1), ("2026-01-01", 1), ("infinity", 1), type="date"),
            "infinity appears more than once in sample_values",
        ),
    ],
)
def test_a_value_sampled_twice_is_a_duplicate(data: object, message: str) -> None:
    found = error(data)
    assert (found.code, found.message) == ("duplicate_value", message)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (sample(("infinity", 1)), "invalid_value"),
        (sample(("Infinity", 1), type="date"), "invalid_value"),
        (sample(("NaN", 1), type="timestamp"), "invalid_value"),
        (sample(("40000", 1), type="int2"), "value_out_of_range"),
        (sample(type="text"), "unsupported_type"),
        (sample(type="numeric"), "unsupported_type"),
        (sample(partitions=0), "invalid_partitions"),
        (sample(column=""), "invalid_column"),
        (sample(("5", 2), sample_null_rows=2, rows_estimate=INT64_MAX), "invalid_rows"),
    ],
)
def test_values_and_results_are_checked_as_in_section_9(data: object, code: str) -> None:
    assert error(data).code == code
