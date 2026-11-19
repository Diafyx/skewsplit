from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pytest

from check_fixtures import check_fixture, check_name, check_plan, main, spec_version

REPO = Path(__file__).parents[4]
FIXTURES = REPO / "spec" / "fixtures"
VERSION = spec_version((REPO / "spec" / "SPEC.md").read_text(encoding="utf-8"))

Fixture = dict[str, Any]


def load(name: str) -> Fixture:
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    fixture: Fixture = json.loads(text, parse_float=Decimal)
    return fixture


def problems_after(name: str, change: Callable[[Fixture], object]) -> list[str]:
    fixture = copy.deepcopy(load(name))
    change(fixture)
    return check_plan(fixture["input"], fixture["expected"])


def test_every_repository_fixture_is_consistent() -> None:
    paths = sorted(FIXTURES.glob("*.json"))
    assert len(paths) >= 12
    assert [p for path in paths for p in check_fixture(path, VERSION)] == []


def test_the_fixtures_cover_every_error_code() -> None:
    codes = {load(path.stem)["expected"]["error"]["code"] for path in FIXTURES.glob("error-*.json")}
    schema = json.loads(
        (REPO / "spec" / "schema" / "fixture.schema.json").read_text(encoding="utf-8")
    )
    assert codes == set(
        schema["$defs"]["error"]["properties"]["error"]["properties"]["code"]["enum"]
    )


def _set(path: str, value: object) -> Callable[[Fixture], object]:
    """Return a change that sets one value, addressed by dotted keys and indexes."""

    def change(fixture: Fixture) -> None:
        *parents, last = path.split(".")
        node: Any = fixture
        for key in parents:
            node = node[int(key)] if key.isdigit() else node[key]
        node[int(last) if last.isdigit() else last] = value

    return change


@pytest.mark.parametrize(
    ("name", "change", "message"),
    [
        ("one-dominant-value", _set("input.column", "id"), "column must be 'id'"),
        ("one-dominant-value", _set("input.source", {"a": 1}), "source must be {'a': 1}"),
        ("numeric-scale", _set("input.scale", 3), "scale must be 3"),
        ("one-dominant-value", _set("input.partitions", 5), "requested_partitions must be 5"),
        ("one-dominant-value", _set("input.null_rows", 99), "rows must be"),
        (
            "one-dominant-value",
            _set("expected.predicates.0.estimated_rows", 99),
            "the NULL predicate",
        ),
        ("one-dominant-value", _set("expected.predicates.4.estimated_rows", 193), "add up"),
        (
            "one-dominant-value",
            _set("expected.planned_partitions", 3),
            "planned_partitions must be 4",
        ),
        ("one-dominant-value", _set("input.partitions", 3), "never has more than N"),
        ("one-dominant-value", _set("expected.skew_score", Decimal("1.886")), "must be 1.887"),
        ("one-dominant-value", _set("expected.skew_score", None), "must be 1.887"),
        ("no-rows", _set("expected.skew_score", Decimal("0.0")), "must be null"),
    ],
)
def test_plan_must_agree_with_its_input(
    name: str, change: Callable[[Fixture], object], message: str
) -> None:
    assert any(message in problem for problem in problems_after(name, change))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_set("expected.predicates.1.lower", {"op": ">=", "value": "0"}), "open below"),
        (_set("expected.predicates.4.upper", {"op": "<", "value": "900"}), "open above"),
        (_set("expected.predicates.1.upper", {"op": "<", "value": "41"}), "does not end at = 42"),
        (_set("expected.predicates.3.lower", {"op": ">=", "value": "42"}), "does not start after"),
        (_set("expected.predicates.4.lower", {"op": ">=", "value": "521"}), "does not start where"),
        (_set("expected.predicates.3.upper", None), "does not start where"),
    ],
)
def test_predicates_must_fit_together(change: Callable[[Fixture], object], message: str) -> None:
    assert any(message in problem for problem in problems_after("one-dominant-value", change))


def test_two_value_predicates_need_a_range_between() -> None:
    def drop_range(fixture: Fixture) -> None:
        del fixture["expected"]["predicates"][3]

    problems = problems_after("fewer-distinct-values", drop_range)
    assert "no range between = 1 and = 2" in problems


@pytest.mark.parametrize(
    ("name", "index", "message"),
    [
        ("several-dominant-values", 0, "dominant_value warnings must name exactly"),
        ("repeated-bounds", 0, "fewer_partitions must be given"),
        ("null-heavy-date", 0, "nulls_exceed_share must be given"),
        ("no-rows", 1, "no_rows must be given"),
    ],
)
def test_each_warning_must_match_the_plan(name: str, index: int, message: str) -> None:
    def drop_warning(fixture: Fixture) -> None:
        del fixture["expected"]["warnings"][index]

    assert any(message in problem for problem in problems_after(name, drop_warning))


def test_warnings_must_be_in_spec_order() -> None:
    def swap(fixture: Fixture) -> None:
        fixture["expected"]["warnings"].reverse()

    problems = problems_after("several-dominant-values", swap)
    assert any("out of order" in problem for problem in problems)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("no-rows", "no_rows is given more than once"),
        ("uniform-int4", "no_rows must be given exactly when total_rows is 0"),
    ],
)
def test_an_extra_warning_is_reported(name: str, message: str) -> None:
    def append(fixture: Fixture) -> None:
        fixture["expected"]["warnings"].append({"code": "no_rows"})

    assert message in problems_after(name, append)


@pytest.mark.parametrize(
    ("name", "is_error", "expected"),
    [
        ("uniform-int4.json", False, []),
        ("error-invalid-value.json", True, []),
        ("Uniform_int4.json", False, ["lowercase words"]),
        ("uniform-int4.json5", False, ["lowercase words"]),
        ("invalid-value.json", True, ["error fixtures"]),
        ("error-x.json", False, ["error fixtures"]),
    ],
)
def test_check_name(name: str, *, is_error: bool, expected: list[str]) -> None:
    problems = check_name(name, is_error=is_error)
    assert len(problems) == len(expected)
    assert all(word in problem for word, problem in zip(expected, problems))


@pytest.mark.parametrize("count", [0, 2])
def test_spec_version_needs_exactly_one_line(count: int) -> None:
    with pytest.raises(ValueError, match=f"found {count}"):
        spec_version("Spec version: **1.0**\n" * count)


def test_a_fixture_for_another_spec_version_is_reported() -> None:
    path = FIXTURES / "error-duplicate-value.json"
    message = "spec_version must be 2.0, the version in spec/SPEC.md"
    assert check_fixture(path, "2.0") == [f"{path}: {message}"]


def test_main_reports_problems_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "notes.txt").write_text("x\n", encoding="utf-8")
    fixture = load("uniform-int4")
    fixture["expected"]["planned_partitions"] = 3
    (tmp_path / "uniform-int4.json").write_text(json.dumps(fixture, default=str), encoding="utf-8")
    monkeypatch.chdir(REPO)
    assert main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "only .json fixtures belong" in err
    assert "planned_partitions must be 4" in err


def test_main_checks_the_array_text_fixtures_without_a_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pg_array = tmp_path / "pg-array"
    pg_array.mkdir()
    (tmp_path / "other").mkdir()
    (pg_array / "notes.txt").write_text("x\n", encoding="utf-8")
    fixture = {
        "description": "d",
        "input": {"text": "{}", "type": "int4"},
        "expected": {"spec_version": "1.0", "error": {"code": "invalid_array"}},
    }
    (pg_array / "bad.json").write_text(json.dumps(fixture), encoding="utf-8")
    fixture["expected"] = {"spec_version": "0.9", "values": []}
    (pg_array / "empty.json").write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.chdir(REPO)
    assert main([str(tmp_path)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err == [
        f"{tmp_path / 'other'}: only .json fixtures belong in {tmp_path}",
        (
            f"{pg_array / 'bad.json'}: error fixtures, and only they, have names starting"
            " with 'error-'"
        ),
        f"{pg_array / 'empty.json'}: spec_version must be 1.0, the version in spec/SPEC.md",
        f"{pg_array / 'notes.txt'}: only .json fixtures belong in {pg_array}",
    ]


def test_main_passes_on_the_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO)
    assert main([]) == 0
    assert "consistent with spec version 1.0" in capsys.readouterr().out
