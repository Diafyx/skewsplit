from __future__ import annotations

import doctest
import json
from typing import Any

import pytest

from skewsplit.core import planner
from skewsplit.core.errors import SpecError
from skewsplit.core.inputs import parse_input
from skewsplit.core.plan import Plan, error_dict
from skewsplit.core.planner import Estimate, _allocate, plan


def make(
    bounds: list[str] | None = None,
    rows: int = 0,
    mcv: dict[str, int] | None = None,
    **fields: object,
) -> dict[str, Any]:
    data: dict[str, Any] = {"column": "c", "type": "int4", "partitions": 4, "null_rows": 0}
    data["mcv"] = [{"value": value, "rows": count} for value, count in (mcv or {}).items()]
    data["histogram"] = None if bounds is None else {"bounds": bounds, "rows": rows}
    data.update(fields)
    return data


def table(result: Plan) -> list[tuple[str, int]]:
    """The non-NULL predicates with their estimated rows."""
    return [(p.sql.replace('"c" ', ""), p.estimated_rows) for p in result.parts[1:]]


def codes(result: Plan) -> list[str]:
    return [w.code for w in result.warnings]


SECTION_1 = make(
    ["1", "250", "500", "750", "1000"],
    400,
    {"7": 60, "42": 500},
    column="customer_id",
    null_rows=100,
)


def test_the_example_of_section_10() -> None:
    expected = {
        "spec_version": "1.1",
        "column": "customer_id",
        "type": "int4",
        "requested_partitions": 4,
        "planned_partitions": 4,
        "rows": {"total": 1060, "null": 100, "non_null": 960},
        "predicates": [
            {"kind": "null", "sql": '"customer_id" IS NULL', "estimated_rows": 100},
            {
                "kind": "range",
                "lower": None,
                "upper": {"op": "<", "value": "42"},
                "sql": '"customer_id" < 42',
                "estimated_rows": 76,
            },
            {"kind": "value", "value": "42", "sql": '"customer_id" = 42', "estimated_rows": 500},
            {
                "kind": "range",
                "lower": {"op": ">", "value": "42"},
                "upper": {"op": "<", "value": "520"},
                "sql": '"customer_id" > 42 AND "customer_id" < 520',
                "estimated_rows": 192,
            },
            {
                "kind": "range",
                "lower": {"op": ">=", "value": "520"},
                "upper": None,
                "sql": '"customer_id" >= 520',
                "estimated_rows": 192,
            },
        ],
        "skew_score": 1.887,
        "warnings": [{"code": "dominant_value", "value": "42", "rows": 500}],
        "source": None,
    }
    out = plan(SECTION_1).to_dict()
    warnings = out["warnings"]
    assert isinstance(warnings, list)
    assert "cannot be split further" in warnings[0].pop("message")
    assert out == expected
    assert json.dumps(out["skew_score"]) == "1.887"


# Section 3: G, the rows below x.
@pytest.mark.parametrize(
    ("bounds", "rows", "mcv", "points"),
    [
        (["1", "10", "20", "1000000"], 900, {}, {8: 233, 7: 200, 1: 0, 0: 0, 10: 300, 10**7: 900}),
        (["1", "10", "20", "1000000"], 1000, {}, {10: 333, 20: 666, 1000000: 1000}),
        (["1", "5", "5", "5", "100"], 400, {}, {4: 75, 5: 100, 6: 301, 100: 400}),
        (["5", "5"], 10, {}, {5: 0, 6: 10}),
        (None, 0, {"3": 4, "9": 6}, {3: 0, 4: 4, 9: 4, 10: 10}),
    ],
)
def test_estimate(
    bounds: list[str] | None, rows: int, mcv: dict[str, int], points: dict[int, int]
) -> None:
    g = Estimate(parse_input(make(bounds, rows, mcv)), frozenset())
    assert {x: g.below(x) for x in points} == points
    assert (g.at(None, upper=False), g.at(None, upper=True)) == (0, rows + sum(mcv.values()))


def test_estimate_leaves_out_dominant_values() -> None:
    g = Estimate(parse_input(make(None, 0, {"3": 4, "9": 6})), frozenset({3}))
    assert (g.below(10), g.at(None, upper=True)) == (6, 6)


# Section 4.1: dominant values.
@pytest.mark.parametrize(
    ("n", "mcv", "dominant", "not_isolated"),
    [
        (4, {"7": 60, "42": 500}, ["42"], []),
        (2, {"1": 700}, [], ["1"]),
        (6, {"7": 3000, "42": 2500, "99": 1700}, ["7", "42"], ["99"]),
        # 300 * 4 is equal to R, so 10 is a candidate; 20 wins the tie with 30.
        (4, {"10": 300, "20": 400, "30": 400}, ["20"], ["10", "30"]),
        (3, {"5": 1000}, ["5"], []),
    ],
)
def test_dominant_values(
    n: int, mcv: dict[str, int], dominant: list[str], not_isolated: list[str]
) -> None:
    rest = 1200 - sum(mcv.values()) if n == 4 and len(mcv) == 3 else 400
    result = plan(make(["1000", "2000"], rest, mcv, partitions=n))
    found = {code: [w.value for w in result.warnings if w.code == code] for code in codes(result)}
    assert found.get("dominant_value", []) == dominant
    assert found.get("dominant_value_not_isolated", []) == not_isolated
    assert [p.value for p in result.parts if p.kind == "value"] == dominant


# Section 4.2: sharing the ranges among segments.
@pytest.mark.parametrize(
    ("sizes", "ranges", "counts"),
    [
        ([0, 88, 212], 4, [1, 1, 2]),
        ([0, 0], 3, [2, 1]),
        ([100, 60], 4, [2, 2]),
        ([100, 60], 5, [3, 2]),
        ([7], 1, [1]),
        # After 100 gets a second range, both shares are 50: the lower segment wins.
        ([50, 100], 4, [2, 2]),
    ],
)
def test_allocate(sizes: list[int], ranges: int, counts: list[int]) -> None:
    assert _allocate(sizes, ranges) == counts


# Section 4.3: split points.
def test_nearest_candidate() -> None:
    result = plan(make(["1", "10", "20", "1000000"], 900))
    assert table(result) == [
        ("< 8", 233),
        (">= 8 AND < 15", 217),
        (">= 15 AND < 250015", 225),
        (">= 250015", 225),
    ]


def test_targets_round_down() -> None:
    # 3 rows over 0 to 3 for 2 ranges: the target is 3 / 2 = 1, not 2.
    assert table(plan(make(["0", "3"], 3, partitions=2))) == [("< 1", 1), (">= 1", 2)]


def test_tie_between_candidates_takes_x_hi() -> None:
    result = plan(make(["0", "4"], 4, {"5": 4, "9": 4}, partitions=2))
    assert table(result) == [("< 6", 8), (">= 6", 4)]


def test_split_point_at_the_type_limit() -> None:
    mcv = {"0": 100, "50": 30, "32767": 30}
    result = plan(make(["1", "100"], 20, mcv, type="int2", partitions=5))
    assert table(result) == [
        ("< 0", 0),
        ("= 0", 100),
        ("> 0 AND < 51", 40),
        (">= 51 AND < 32767", 10),
        (">= 32767", 30),
    ]


def test_dropped_split_point_gives_fewer_partitions() -> None:
    result = plan(make(["1", "5", "5", "5", "100"], 400))
    assert table(result) == [("< 5", 100), (">= 5 AND < 6", 201), (">= 6", 99)]
    assert result.warnings[0].to_dict()["requested"] == 4
    assert (result.planned_partitions, codes(result)) == (3, ["fewer_partitions"])


def test_x_lo_is_not_a_candidate_at_the_lower_limit() -> None:
    # Above dominant value 1, the first ordinal, 2, already reaches the target.
    result = plan(make(["0", "2"], 1000, {"1": 2000}, partitions=5))
    assert table(result) == [("< 1", 500), ("= 1", 2000), ("> 1", 500)]


# Section 4.4: fewer partitions.
def test_fewer_distinct_values_than_partitions() -> None:
    result = plan(make(None, 0, {"1": 100, "2": 100, "3": 100}, type="int2", partitions=8))
    assert table(result) == [
        ("< 1", 0),
        ("= 1", 100),
        ("> 1 AND < 2", 0),
        ("= 2", 100),
        ("> 2 AND < 3", 0),
        ("= 3", 100),
        ("> 3", 0),
    ]
    assert codes(result)[-1] == "fewer_partitions"


# Section 5: predicates and rendering.
def test_one_partition_is_is_not_null() -> None:
    result = plan(make(["1", "500", "1000"], 1000, null_rows=5, partitions=1))
    assert table(result) == [("IS NOT NULL", 1000)]
    assert (result.skew_score, codes(result)) == (0.995, [])


@pytest.mark.parametrize(
    ("column", "quoted"),
    [("customer_id", '"customer_id"'), ('a"b', '"a""b"'), ("Order Id", '"Order Id"')],
)
def test_column_is_quoted(column: str, quoted: str) -> None:
    result = plan(make(["1", "3"], 2, column=column, partitions=2))
    assert [p.sql for p in result.parts] == [
        f"{quoted} IS NULL",
        f"{quoted} < 2",
        f"{quoted} >= 2",
    ]


@pytest.mark.parametrize(
    ("type_name", "bounds", "sql"),
    [
        ("date", ["2026-01-01", "2026-01-03"], "\"c\" < '2026-01-02'::date"),
        (
            "timestamp",
            ["2026-11-18 00:00:00", "2026-11-18 00:00:01"],
            "\"c\" < '2026-11-18 00:00:00.5'::timestamp",
        ),
        (
            "timestamptz",
            ["2026-11-18 00:00:00+00", "2026-11-18 02:00:00+00"],
            "\"c\" < '2026-11-18 01:00:00+00'::timestamptz",
        ),
    ],
)
def test_dates_and_timestamps_are_quoted_with_a_cast(
    type_name: str, bounds: list[str], sql: str
) -> None:
    result = plan(make(bounds, 2, type=type_name, partitions=2))
    assert result.parts[1].sql == sql


def test_numeric_keeps_its_scale() -> None:
    result = plan(make(["0", "1"], 2, type="numeric", scale=2, partitions=2))
    out = result.to_dict()
    assert (out["type"], out["scale"]) == ("numeric", 2)
    assert table(result) == [("< 0.50", 1), (">= 0.50", 1)]
    assert "scale" not in plan(SECTION_1).to_dict()


# Section 7: skew score.
def test_skew_score_of_an_empty_table_is_null() -> None:
    result = plan(make(None))
    assert table(result) == [("IS NOT NULL", 0)]
    assert (result.skew_milli, result.skew_score) == (None, None)
    assert codes(result) == ["fewer_partitions", "no_rows"]
    assert result.to_dict()["skew_score"] is None


def test_skew_score_rounds_half_up() -> None:
    # 233 * 4 / 900 = 1.03555..., and 8 * 2 / 12 = 1.3333...
    assert plan(make(["1", "10", "20", "1000000"], 900)).skew_score == 1.036
    assert plan(make(["0", "4"], 4, {"5": 4, "9": 4}, partitions=2)).skew_score == 1.333


# Section 8: warnings.
def test_nulls_exceed_share() -> None:
    bounds = ["2026-01-01", "2026-04-01", "2026-07-01", "2026-10-01"]
    result = plan(make(bounds, 300, type="date", null_rows=700, partitions=3))
    assert [w.to_dict() for w in result.warnings] == [
        {"code": "nulls_exceed_share", "rows": 700, "message": result.warnings[0].message}
    ]
    assert result.skew_score == 2.1


def test_all_null() -> None:
    result = plan(make(None, null_rows=10, partitions=3))
    assert [p.sql for p in result.parts] == ['"c" IS NULL', '"c" IS NOT NULL']
    assert codes(result) == ["fewer_partitions", "nulls_exceed_share"]
    assert result.skew_score == 3.0


def test_source_is_copied() -> None:
    source = {"strategy": "pg_stats", "analyzed": "2026-11-18 00:00:00+00"}
    assert plan(make(["1", "3"], 2, source=source)).to_dict()["source"] == source


def test_invalid_input_raises_spec_error() -> None:
    with pytest.raises(SpecError) as caught:
        plan(make(partitions=0))
    assert error_dict(caught.value) == {
        "spec_version": "1.1",
        "error": {"code": "invalid_partitions", "message": caught.value.message},
    }


def test_module_examples() -> None:
    assert doctest.testmod(planner).failed == 0
