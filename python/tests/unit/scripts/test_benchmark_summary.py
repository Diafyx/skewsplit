from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from benchmark_summary import duration, main, render

if TYPE_CHECKING:
    from pathlib import Path

REPORT = {
    "machine_info": {"python_version": "3.15.2", "cpu": {"brand_raw": "Some CPU"}},
    "benchmarks": [
        {
            "name": "test_plan_small",
            "stats": {"median": 5.869e-05, "min": 5.528e-05, "rounds": 6599},
        },
        {"name": "test_plan_large", "stats": {"median": 0.6122, "min": 0.6072, "rounds": 3}},
    ],
}


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(2.5, "2.5 s"), (0.6122, "612 ms"), (5.869e-05, "58.7 us"), (1e-3, "1 ms"), (4e-7, "0.4 us")],
)
def test_duration(seconds: float, text: str) -> None:
    assert duration(seconds) == text


def test_render_lists_each_benchmark_and_the_machine() -> None:
    assert render("Planner", REPORT).splitlines() == [
        "### Planner",
        "",
        "| Benchmark | Median | Min | Rounds |",
        "|-----------|-------:|----:|-------:|",
        "| `test_plan_small` | 58.7 us | 55.3 us | 6599 |",
        "| `test_plan_large` | 612 ms | 607 ms | 3 |",
        "",
        "Python 3.15.2 on Some CPU. Timings inform; they do not gate.",
    ]


@pytest.mark.parametrize("report", [None, {}, {"benchmarks": []}])
def test_render_says_when_there_are_no_results(report: dict[str, object] | None) -> None:
    assert render("Planner", report).endswith("No benchmark results were recorded.\n")


def test_render_without_machine_info() -> None:
    assert render("Planner", {"benchmarks": REPORT["benchmarks"]}).endswith("| 3 |\n")


def test_main_appends_to_the_step_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report, summary = tmp_path / "bench.json", tmp_path / "summary.md"
    report.write_text(json.dumps(REPORT), encoding="utf-8")
    summary.write_text("earlier\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["Planner", str(report)]) == 0
    assert summary.read_text(encoding="utf-8") == "earlier\n" + capsys.readouterr().out


def test_main_reports_a_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main(["Planner", str(tmp_path / "missing.json")]) == 0
    assert "No benchmark results" in capsys.readouterr().out
