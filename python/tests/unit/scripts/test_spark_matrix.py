from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from spark_matrix import LINES, PYTHONS, legs, main

ROOT = Path(__file__).parents[4]

# The legs every pull request runs, as ci.yml listed them before the script.
PULL_REQUEST_LEGS = [
    ("3.5", "3.9", "17"),
    ("3.5", "3.11", "17"),
    ("4.1", "3.10", "17"),
    ("4.1", "3.10", "21"),
    ("4.1", "3.14", "17"),
    ("4.1", "3.14", "21"),
    ("4.2", "3.10", "17"),
    ("4.2", "3.10", "21"),
    ("4.2", "3.14", "17"),
    ("4.2", "3.14", "21"),
    ("4.3", "3.11", "17"),
    ("4.3", "3.11", "21"),
    ("4.3", "3.14", "17"),
    ("4.3", "3.14", "21"),
]


def triples(found: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    return [(leg["spark"], leg["python"], leg["java"]) for leg in found]


def test_pull_requests_run_the_oldest_and_newest_python_of_each_line() -> None:
    assert triples(legs(every_python=False)) == PULL_REQUEST_LEGS


def test_every_python_adds_the_pythons_in_between() -> None:
    found = triples(legs(every_python=True))
    assert len(found) == len(set(found)) == 31
    assert set(PULL_REQUEST_LEGS) < set(found)
    assert [leg for leg in found if leg[0] == "3.5"] == [
        ("3.5", "3.9", "17"),
        ("3.5", "3.10", "17"),
        ("3.5", "3.11", "17"),
    ]
    assert {python for spark, python, _ in found if spark == "4.3"} == {
        "3.11",
        "3.12",
        "3.13",
        "3.14",
    }


def test_the_lines_and_pythons_match_the_supported_versions() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    spark = re.search(r"^\| Spark \| ([^|]+) \|", readme, re.MULTILINE)
    python = re.search(r"^\| Python \| (\S+) to (\S+) \|", readme, re.MULTILINE)
    assert spark is not None
    assert python is not None
    assert list(LINES) == spark.group(1).strip().split(", ")
    assert (PYTHONS[0], PYTHONS[-1]) == python.groups()
    assert [int(version.split(".")[1]) for version in PYTHONS] == list(range(9, 16))
    for oldest, newest, javas in LINES.values():
        assert PYTHONS.index(oldest) < PYTHONS.index(newest)
        assert set(javas) <= {"17", "21"}


def test_the_readme_lists_the_python_range_of_each_line() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    row = re.search(r"^\| Python for Spark \| ([^|]+) \|", readme, re.MULTILINE)
    assert row is not None
    ranges: dict[tuple[str, str], list[str]] = {}
    for spark, (oldest, newest, _) in LINES.items():
        ranges.setdefault((oldest, newest), []).append(spark)
    expected = "; ".join(
        f"{oldest} to {newest} for {' and '.join(lines)}"
        for (oldest, newest), lines in ranges.items()
    ).replace(" for ", " for Spark ", 1)
    assert row.group(1).strip() == expected


def test_each_line_has_its_pinned_pyspark() -> None:
    folders = sorted((ROOT / "python" / "tests" / "spark").glob("pyspark-*/requirements.txt"))
    assert [path.parent.name.removeprefix("pyspark-") for path in folders] == list(LINES)


def test_ci_takes_its_spark_legs_from_the_script() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    nightly = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    assert "      spark-matrix: ${{ steps.spark.outputs.spark-matrix }}\n" in ci
    assert "python3 scripts/spark_matrix.py" in ci
    assert "      matrix: ${{ fromJSON(needs.changes.outputs.spark-matrix) }}\n" in ci
    assert "      every-python: true\n" in nightly


@pytest.mark.parametrize(("argv", "count"), [([], 14), (["--every-python"], 31)])
def test_main_prints_and_appends_to_github_output(
    argv: list[str],
    count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "output"
    output.write_text("earlier=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main(argv) == 0
    printed = capsys.readouterr().out
    assert output.read_text(encoding="utf-8") == f"earlier=1\n{printed}"
    name, _, value = printed.rstrip("\n").partition("=")
    assert name == "spark-matrix"
    assert len(json.loads(value)["include"]) == count


def test_main_prints_without_github_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert main([]) == 0
    assert capsys.readouterr().out.startswith('spark-matrix={"include":[{"spark":"3.5",')
