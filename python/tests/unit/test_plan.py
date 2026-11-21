from __future__ import annotations

import json
from dataclasses import replace

import pytest

from skewsplit.core.planner import plan

SECTION_1 = {
    "column": "customer_id",
    "type": "int4",
    "partitions": 4,
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
}
SUMMARY = (
    "customer_id (int4): 1060 estimated rows, 4 of 4 partitions plus one for NULL, skew score 1.887"
)
DOMINANT = (
    "warning dominant_value: 42 holds 500 rows, at least one partition's share;"
    " it cannot be split further on this column"
)


def test_predicates_are_the_sql_of_each_part_as_a_list() -> None:
    result = plan(SECTION_1)
    # A list, as PySpark's DataFrameReader.jdbc declares its predicates.
    assert isinstance(result.predicates, list)
    assert result.predicates == [part.sql for part in result.parts]
    assert result.predicates[2] == '"customer_id" = 42'


def test_the_report_of_section_1() -> None:
    report = plan(SECTION_1).report
    assert report.split("\n") == [
        SUMMARY,
        "rows  predicate",
        ' 100  "customer_id" IS NULL',
        '  76  "customer_id" < 42',
        ' 500  "customer_id" = 42',
        ' 192  "customer_id" > 42 AND "customer_id" < 520',
        ' 192  "customer_id" >= 520',
        DOMINANT,
    ]


def test_the_report_aligns_wide_row_counts_and_shows_the_scale() -> None:
    result = plan(
        {
            "column": "amount",
            "type": "numeric",
            "scale": 2,
            "partitions": 2,
            "null_rows": 7,
            "mcv": [],
            "histogram": {"bounds": ["1.00", "3.00"], "rows": 20000},
        }
    )
    summary = (
        "amount (numeric, scale 2): 20007 estimated rows, 2 of 2 partitions plus one for NULL,"
        " skew score 1.000"
    )
    assert result.report.split("\n") == [
        summary,
        " rows  predicate",
        '    7  "amount" IS NULL',
        '10000  "amount" < 2.00',
        '10000  "amount" >= 2.00',
    ]


def test_the_report_of_a_plan_without_rows() -> None:
    empty = {"column": "c", "type": "int4", "partitions": 3, "null_rows": 0, "mcv": []}
    lines = plan({**empty, "histogram": None}).report.split("\n")
    assert lines[0] == (
        "c (int4): 0 estimated rows, 1 of 3 partitions plus one for NULL, no skew score"
    )
    assert lines[1:4] == ["rows  predicate", '   0  "c" IS NULL', '   0  "c" IS NOT NULL']
    assert [line.split(":")[0] for line in lines[4:]] == [
        "warning fewer_partitions",
        "warning no_rows",
    ]


def test_the_report_adds_the_well_formed_source_warnings() -> None:
    result = replace(
        plan(SECTION_1),
        source={
            "statistics": "pg_stats",
            "warnings": [
                {"code": "stale_statistics", "modified_rows": 200, "rows": 1060, "message": "old"},
                "not a warning",
                {"code": 7, "message": "a code that is not text"},
                {"code": "no_message"},
            ],
        },
    )
    assert result.report.split("\n")[-2:] == [DOMINANT, "warning stale_statistics: old"]


@pytest.mark.parametrize(
    "source", [None, "pg_stats", {"statistics": "pg_stats"}, {"warnings": "none"}]
)
def test_a_source_without_a_list_of_warnings_adds_none(source: object) -> None:
    result = replace(plan(SECTION_1), source=source)
    assert result.report == plan(SECTION_1).report


def test_to_json_is_to_dict_as_json_text() -> None:
    result = plan({**SECTION_1, "column": "größe", "source": {"table": "Straße"}})
    text = result.to_json()
    assert json.loads(text) == result.to_dict()
    assert text == json.dumps(result.to_dict(), ensure_ascii=False)
    assert '"column": "größe"' in text
    assert "\n" not in text
    assert result.to_json(indent=2) == json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


@pytest.mark.parametrize(
    ("source", "error"),
    [({"n_distinct": float("nan")}, ValueError), ({"at": object()}, TypeError)],
)
def test_to_json_refuses_a_source_json_cannot_hold(source: object, error: type[Exception]) -> None:
    result = replace(plan(SECTION_1), source=source)
    with pytest.raises(error):
        result.to_json()
