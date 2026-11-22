from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest

from bench_compare import THRESHOLD, baseline, compare, main, max_tasks

if TYPE_CHECKING:
    from pathlib import Path


def run(method: str, column: str, max_ms: int) -> dict[str, Any]:
    tasks = [{"index": 0, "ms": max_ms, "rows": 5}, {"index": 1, "ms": 10, "rows": 5}]
    return {
        "column": column,
        "method": method,
        "run": 1,
        "plan_seconds": 0.1,
        "seconds": 9.0,
        "tasks": tasks,
    }


REPORT: dict[str, Any] = {
    "rows": 10,
    "partitions": 2,
    "columns": {"customer_id": "bigint", "created_at": "timestamp"},
    "run_url": "https://github.com/o/r/actions/runs/1",
    "results": [
        run("hash-mod", "customer_id", 9000),
        run("skewsplit statistics", "customer_id", 4000),
        run("skewsplit sample", "customer_id", 5000),
        run("skewsplit statistics", "created_at", 2000),
        run("skewsplit sample", "created_at", 2500),
    ],
}
BASELINE = baseline(REPORT)


def moved(seconds: float, column: str = "customer_id") -> dict[str, Any]:
    """REPORT with the statistics strategy's max task time on ``column`` changed."""
    report = copy.deepcopy(REPORT)
    for result in report["results"]:
        if (result["column"], result["method"]) == (column, "skewsplit statistics"):
            result["tasks"][0]["ms"] = int(seconds * 1000)
    report["run_url"] = "https://github.com/o/r/actions/runs/2"
    return report


def test_max_tasks_are_skewsplits_medians() -> None:
    assert max_tasks(REPORT) == {
        "customer_id": {"skewsplit statistics": 4.0, "skewsplit sample": 5.0},
        "created_at": {"skewsplit statistics": 2.0, "skewsplit sample": 2.5},
    }


def test_the_baseline() -> None:
    assert {
        "run_url": "https://github.com/o/r/actions/runs/1",
        "rows": 10,
        "partitions": 2,
        "max_task_seconds": max_tasks(REPORT),
    } == BASELINE


@pytest.mark.parametrize(
    ("seconds", "changed"),
    [(4.0, False), (4.4, False), (3.6, False), (4.41, True), (3.59, True), (8.0, True)],
)
def test_a_change_of_more_than_ten_percent_either_way(seconds: float, *, changed: bool) -> None:
    assert THRESHOLD == 10
    found, body = compare(BASELINE, moved(seconds))
    assert found is changed
    change = (seconds - 4.0) / 4.0 * 100
    row = f"| `customer_id` | skewsplit statistics | 4.0 s | {seconds:.1f} s | {change:+.0f}%"
    assert row in body
    assert ("**over the threshold**" in body) is changed


def test_the_body_links_both_runs() -> None:
    _, body = compare(BASELINE, moved(9.0))
    assert body.startswith(
        "skewsplit's median max task time in the nightly benchmark, against the baseline"
        " from https://github.com/o/r/actions/runs/1. A change of more than 10% either way"
        " opens this issue.\n"
    )
    assert body.endswith(
        "This run, with `bench-results` as its artifact: https://github.com/o/r/actions/runs/2\n"
    )


def test_a_missing_method_is_a_change() -> None:
    report = copy.deepcopy(REPORT)
    report["results"] = [r for r in report["results"] if r["column"] != "created_at"]
    report["run_url"] = None
    changed, body = compare(BASELINE, report)
    assert changed
    assert "| `created_at` | skewsplit sample | 2.5 s | missing | |" in body
    assert "This run, with" not in body


def test_another_table_size_does_not_compare() -> None:
    changed, body = compare(BASELINE, {**REPORT, "rows": 20})
    assert changed
    assert "The baseline read 10 rows in 2 partitions, this run 20 in 2" in body


def test_a_baseline_without_a_run_says_so() -> None:
    _, body = compare({**BASELINE, "run_url": None}, REPORT)
    assert "against the baseline from the committed baseline." in body


def test_main_compares_and_sets_the_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, report, body, output = (tmp_path / name for name in ("b.json", "r.json", "b.md", "out"))
    base.write_text(json.dumps(BASELINE), encoding="utf-8")
    report.write_text(json.dumps(moved(9.0)), encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main([str(base), str(report), str(body)]) == 0
    assert output.read_text(encoding="utf-8") == "changed=true\n"
    assert body.read_text(encoding="utf-8") == compare(BASELINE, moved(9.0))[1]
    report.write_text(json.dumps(REPORT), encoding="utf-8")
    assert main([str(base), str(report), str(body)]) == 0
    assert output.read_text(encoding="utf-8") == "changed=true\nchanged=false\n"


def test_main_without_an_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, report, body = (tmp_path / name for name in ("b.json", "r.json", "b.md"))
    base.write_text(json.dumps(BASELINE), encoding="utf-8")
    report.write_text(json.dumps(REPORT), encoding="utf-8")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert main([str(base), str(report), str(body)]) == 0
    assert body.is_file()


def test_main_writes_the_baseline(tmp_path: Path) -> None:
    report, base = tmp_path / "r.json", tmp_path / "baseline.json"
    report.write_text(json.dumps(REPORT), encoding="utf-8")
    assert main(["--baseline", str(report), str(base)]) == 0
    assert json.loads(base.read_text(encoding="utf-8")) == BASELINE
