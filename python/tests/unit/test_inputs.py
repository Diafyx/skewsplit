from __future__ import annotations

import copy
from typing import Any

import pytest

from skewsplit.core.errors import SpecError
from skewsplit.core.inputs import parse_input

# The input of spec section 1.
BASE: dict[str, Any] = {
    "column": "customer_id",
    "type": "int4",
    "partitions": 4,
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
}


def changed(**fields: object) -> dict[str, Any]:
    data = copy.deepcopy(BASE)
    for key, value in fields.items():
        if value is DROP:
            del data[key]
        else:
            data[key] = value
    return data


DROP = object()


def test_parse_converts_values_to_sorted_ordinals() -> None:
    inp = parse_input(changed(mcv=[{"value": "42", "rows": 500}, {"value": "7", "rows": 60}]))
    assert inp.mcv == ((7, 60), (42, 500))
    assert inp.bounds == (1, 250, 500, 750, 1000)
    assert (inp.column, inp.partitions, inp.null_rows, inp.histogram_rows) == (
        "customer_id",
        4,
        100,
        400,
    )
    assert inp.codec.type_name == "int4"
    assert inp.source is None


def test_parse_accepts_no_histogram_a_source_and_a_numeric_scale() -> None:
    inp = parse_input(changed(type="numeric", scale=1, histogram=None, source={"a": [1]}))
    assert (inp.bounds, inp.histogram_rows, inp.source) == ((), 0, {"a": [1]})
    assert inp.mcv == ((70, 60), (420, 500))


def test_parse_accepts_repeated_bounds_and_the_largest_total() -> None:
    histogram = {"bounds": ["1", "5", "5", "9"], "rows": 2**63 - 1 - 660}
    assert parse_input(changed(histogram=histogram)).bounds == (1, 5, 5, 9)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "must be a JSON object"),
        (changed(histogram=DROP), "missing histogram"),
        (changed(column=DROP, mcv=DROP), "missing column, mcv"),
        (changed(table="t"), "unknown fields: table"),
        (changed(column=1), "column must be a JSON string"),
        (changed(type=None), "type must be a JSON string"),
        (changed(partitions=4.0), "partitions must be a JSON integer"),
        (changed(partitions=True), "partitions must be a JSON integer"),
        (changed(null_rows="100"), "null_rows must be a JSON integer"),
        (changed(type="numeric", scale="2"), "scale must be a JSON integer"),
        (changed(mcv={"value": "7"}), "mcv must be a list"),
        (changed(mcv=["7"]), "each mcv entry must be an object"),
        (changed(mcv=[{"value": "7"}]), "an mcv entry is missing rows"),
        (changed(mcv=[{"value": "7", "rows": 1, "share": 0.1}]), "unknown fields: share"),
        (changed(mcv=[{"value": 7, "rows": 1}]), "value must be a JSON string"),
        (changed(mcv=[{"value": "7", "rows": False}]), "rows must be a JSON integer"),
        (changed(histogram=["1", "2"]), "histogram must be an object or null"),
        (changed(histogram={"bounds": ["1", "2"]}), "the histogram is missing rows"),
        (changed(histogram={"bounds": "1,2", "rows": 1}), "bounds must be a list of strings"),
        (changed(histogram={"bounds": [1, 2], "rows": 1}), "bounds must be a list of strings"),
        (changed(histogram={"bounds": ["1", "2"], "rows": 1.5}), "rows must be a JSON integer"),
    ],
)
def test_wrong_shape_is_invalid_input(data: object, message: str) -> None:
    with pytest.raises(SpecError, match=message) as caught:
        parse_input(data)
    assert caught.value.code == "invalid_input"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (changed(column=""), "invalid_column"),
        (changed(column="a\0b"), "invalid_column"),
        (changed(type="uuid"), "unsupported_type"),
        (changed(type="numeric"), "unsupported_type"),
        (changed(scale=0), "unsupported_type"),
        (changed(partitions=0), "invalid_partitions"),
        (changed(partitions=10001), "invalid_partitions"),
        (changed(null_rows=-1), "invalid_rows"),
        (changed(histogram={"bounds": ["1", "2"], "rows": -1}), "invalid_rows"),
        (changed(mcv=[{"value": "7", "rows": 0}]), "invalid_rows"),
        (changed(mcv=[{"value": "7", "rows": -5}]), "invalid_rows"),
        (changed(null_rows=2**63 - 1 - 960), None),
        (changed(null_rows=2**63 - 960), "invalid_rows"),
        (changed(mcv=[{"value": "07", "rows": 1}]), "invalid_value"),
        (changed(histogram={"bounds": ["1", "x"], "rows": 1}), "invalid_value"),
        (changed(mcv=[{"value": "2147483648", "rows": 1}]), "value_out_of_range"),
        (changed(mcv=[{"value": "7", "rows": 1}, {"value": "7", "rows": 2}]), "duplicate_value"),
        (changed(histogram={"bounds": ["1"], "rows": 1}), "invalid_histogram"),
        (changed(histogram={"bounds": [], "rows": 0}), "invalid_histogram"),
        (changed(histogram={"bounds": ["1", "3", "2"], "rows": 1}), "invalid_histogram"),
        (changed(histogram={"bounds": ["1", "42"], "rows": 1}), "invalid_histogram"),
    ],
)
def test_each_problem_has_its_error_code(data: object, code: str | None) -> None:
    if code is None:
        parse_input(data)
        return
    with pytest.raises(SpecError) as caught:
        parse_input(data)
    assert caught.value.code == code


def test_equal_numeric_values_written_differently_are_duplicates() -> None:
    mcv = [{"value": "12.5", "rows": 1}, {"value": "12.50", "rows": 2}]
    with pytest.raises(SpecError, match=r"12\.50 appears more than once") as caught:
        parse_input(changed(type="numeric", scale=2, mcv=mcv, histogram=None))
    assert caught.value.code == "duplicate_value"


def test_messages_name_the_problem_values() -> None:
    with pytest.raises(SpecError, match="bounds 3 and 2 are out of order"):
        parse_input(changed(histogram={"bounds": ["1", "3", "2"], "rows": 1}))
    with pytest.raises(SpecError, match="the bound 7 is also a common value"):
        parse_input(changed(histogram={"bounds": ["7", "42"], "rows": 1}))
