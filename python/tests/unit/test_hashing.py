"""Hash predicates (spec section 16): every rule at its edges, and the exact errors."""

from __future__ import annotations

from typing import Any

import pytest

from skewsplit.core.errors import SpecError
from skewsplit.core.hashing import hash_plan

BASE: dict[str, Any] = {
    "column": "session_id",
    "type": "uuid",
    "partitions": 4,
    "null_rows": 10,
    "non_null_rows": 1000,
}


def planned(**changes: object) -> Any:  # noqa: ANN401 -- a Plan, kept loose for the tests
    return hash_plan({**BASE, **changes})


def test_the_example_of_the_spec() -> None:
    result = planned()
    assert result.predicates == [
        '"session_id" IS NULL',
        *(
            f'((pg_catalog.hashtextextended("session_id"::text, 0) % 4) + 4) % 4 = {i}'
            for i in range(4)
        ),
    ]
    assert [part.estimated_rows for part in result.parts] == [10, 250, 250, 250, 250]
    assert [part.kind for part in result.parts] == ["null", "hash", "hash", "hash", "hash"]
    assert [part.remainder for part in result.parts] == [None, 0, 1, 2, 3]
    assert (result.total_rows, result.null_rows, result.skew_milli) == (1010, 10, 990)
    assert (result.column, result.type_name, result.scale) == ("session_id", "uuid", None)
    assert (result.requested_partitions, result.planned_partitions) == (4, 4)
    assert result.source is None


def test_the_json_of_a_hash_predicate() -> None:
    assert planned().to_dict()["predicates"][1] == {
        "kind": "hash",
        "remainder": 0,
        "sql": '((pg_catalog.hashtextextended("session_id"::text, 0) % 4) + 4) % 4 = 0',
        "estimated_rows": 250,
    }


@pytest.mark.parametrize(
    ("partitions", "rows", "estimates"),
    [
        (4, 10, [2, 3, 2, 3]),
        (7, 100, [14, 14, 14, 15, 14, 14, 15]),
        (5, 2, [0, 0, 1, 0, 1]),
        (3, 0, [0, 0, 0]),
        (1, 7, [7]),
    ],
)
def test_the_rows_are_spread_like_histogram_buckets(
    partitions: int, rows: int, estimates: list[int]
) -> None:
    result = planned(partitions=partitions, null_rows=0, non_null_rows=rows)
    assert [part.estimated_rows for part in result.parts[1:]] == estimates
    assert result.planned_partitions == partitions


def test_the_column_is_quoted() -> None:
    result = planned(column='a"b', type="character varying(20)")
    assert result.predicates[0] == '"a""b" IS NULL'
    assert result.predicates[1].startswith('((pg_catalog.hashtextextended("a""b"::text, 0)')
    assert result.type_name == "character varying(20)"


def test_the_source_is_copied() -> None:
    source = {"statistics": "count", "rows": 1010}
    assert planned(source=source).source == source


def test_every_hash_plan_warns_that_each_partition_scans_the_table() -> None:
    (warning,) = planned().warnings
    assert (warning.code, warning.rows) == ("full_scans", None)
    assert warning.message == (
        "each of the 4 hash predicates reads the whole table: no index on the column"
        " can serve them, so the read scans the table once per partition"
    )


@pytest.mark.parametrize(
    ("null_rows", "rows", "codes"),
    [
        # 1 * 5 > 3: one NULL row is more than a fifth of 3 rows.
        (1, 2, ["full_scans", "nulls_exceed_share"]),
        # 2 * 5 > 10 is false: exactly one partition's share.
        (2, 8, ["full_scans"]),
        (3, 7, ["full_scans", "nulls_exceed_share"]),
        (0, 0, ["full_scans", "no_rows"]),
    ],
)
def test_the_warnings_and_their_order(null_rows: int, rows: int, codes: list[str]) -> None:
    result = planned(partitions=5, null_rows=null_rows, non_null_rows=rows)
    assert [warning.code for warning in result.warnings] == codes


def test_the_warning_messages() -> None:
    _, nulls = planned(null_rows=100, non_null_rows=0).warnings
    assert (nulls.rows, nulls.message) == (
        100,
        "100 NULL rows are more than one partition's share",
    )
    _, none = planned(null_rows=0, non_null_rows=0).warnings
    assert none.message == "the input shows no rows; the plan still reads rows added later"


@pytest.mark.parametrize(
    ("null_rows", "rows", "skew"),
    [(0, 0, None), (100, 0, 4000), (0, 10, 1200), (1, 9223372036854775806, 1000)],
)
def test_the_skew_score(null_rows: int, rows: int, skew: int | None) -> None:
    assert planned(null_rows=null_rows, non_null_rows=rows).skew_milli == skew


def test_the_largest_total() -> None:
    result = planned(partitions=3, null_rows=1, non_null_rows=2**63 - 2)
    assert result.total_rows == 2**63 - 1


@pytest.mark.parametrize("partitions", [1, 10000])
def test_the_partition_limits(partitions: int) -> None:
    assert planned(partitions=partitions).planned_partitions == partitions


@pytest.mark.parametrize(
    ("data", "code", "message"),
    [
        (["session_id"], "invalid_input", "the input must be a JSON object"),
        (
            {k: v for k, v in BASE.items() if k not in {"type", "non_null_rows"}},
            "invalid_input",
            "the input is missing type, non_null_rows",
        ),
        ({**BASE, "mcv": [], "b": 1}, "invalid_input", "the input has unknown fields: b, mcv"),
        ({**BASE, "column": 1}, "invalid_input", "column must be a JSON string"),
        ({**BASE, "type": None}, "invalid_input", "type must be a JSON string"),
        ({**BASE, "partitions": "4"}, "invalid_input", "partitions must be a JSON integer"),
        ({**BASE, "null_rows": True}, "invalid_input", "null_rows must be a JSON integer"),
        ({**BASE, "non_null_rows": 2.5}, "invalid_input", "non_null_rows must be a JSON integer"),
        (
            {**BASE, "column": ""},
            "invalid_column",
            "the column name must not be empty or contain U+0000",
        ),
        (
            {**BASE, "column": "a\0"},
            "invalid_column",
            "the column name must not be empty or contain U+0000",
        ),
        ({**BASE, "type": ""}, "unsupported_type", "the type must not be empty or contain U+0000"),
        (
            {**BASE, "type": "u\0"},
            "unsupported_type",
            "the type must not be empty or contain U+0000",
        ),
        ({**BASE, "partitions": 0}, "invalid_partitions", "partitions must be 1 to 10000, not 0"),
        (
            {**BASE, "partitions": 10001},
            "invalid_partitions",
            "partitions must be 1 to 10000, not 10001",
        ),
        (
            {**BASE, "null_rows": -1},
            "invalid_rows",
            "null_rows and non_null_rows must not be negative",
        ),
        (
            {**BASE, "non_null_rows": -1},
            "invalid_rows",
            "null_rows and non_null_rows must not be negative",
        ),
        (
            {**BASE, "null_rows": 1, "non_null_rows": 2**63 - 1},
            "invalid_rows",
            "total_rows 9223372036854775808 is above 2^63 - 1",
        ),
    ],
)
def test_errors(data: object, code: str, message: str) -> None:
    with pytest.raises(SpecError) as raised:
        hash_plan(data)
    assert (raised.value.code, raised.value.message) == (code, message)
