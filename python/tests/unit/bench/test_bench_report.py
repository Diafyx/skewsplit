from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from bench_report import gain, main, render, summarise

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def run(method: str, column: str, seconds: float, tasks: list[tuple[int, int]]) -> dict[str, Any]:
    return {
        "column": column,
        "method": method,
        "run": 1,
        "plan_seconds": 0.5,
        "seconds": seconds,
        "tasks": [{"index": i, "ms": ms, "rows": rows} for i, (ms, rows) in enumerate(tasks)],
    }


REPORT: dict[str, Any] = {
    "table": "bench_orders",
    "rows": 1000,
    "partitions": 2,
    "runs": 1,
    "columns": {"customer_id": "bigint"},
    "environment": {
        "spark": "4.3.0",
        "java": "21.0.9",
        "python": "3.14.1",
        "postgres": "18.7",
        "cpus": 4,
        "memory_gb": 16,
        "runner": "ubuntu24 20261116.1",
    },
    "results": [
        run("default bounds", "customer_id", 9.0, [(8000, 900), (1000, 100)]),
        run("hash-mod", "customer_id", 10.0, [(5000, 10), (5000, 495), (5000, 495)]),
        run("quantile ranges", "customer_id", 4.0, [(100, 10), (2000, 490), (3000, 500)]),
    ],
}


def test_gain_is_positive_when_faster() -> None:
    assert gain(10.0, 7.5) == "+25%"
    assert gain(10.0, 12.0) == "-20%"


def test_summarise_takes_medians_over_runs() -> None:
    runs = [
        run("m", "c", 3.0, [(1000, 5), (4000, 15)]),
        run("m", "c", 1.0, [(2000, 5), (2000, 15)]),
        run("m", "c", 2.0, [(9000, 5), (1000, 15)]),
    ]
    assert summarise(runs, 20) == {
        "tasks": 2,
        "rows": (5, 10, 15),
        "share": 0.75,
        "plan": 0.5,
        "max_task": 4.0,
        "job": 2.0,
    }


def test_render() -> None:
    text = render(REPORT)
    assert "- Table `bench_orders`: 1,000 rows, read in full" in text
    assert "(ubuntu24 20261116.1, 4 CPUs, 16 GB)" in text
    assert "## `customer_id` (bigint)" in text
    assert "| default bounds | 2 | 100 / 500 / 900 | 90% | 0.5 s | 8.0 s | 9.0 s |" in text
    assert "| hash-mod | 3 | 10 / 495 / 495 | 50% | 0.5 s | 5.0 s | 10.0 s |" in text
    assert "| quantile ranges | 3 | 10 / 490 / 500 | 50% | 0.5 s | 3.0 s | 4.0 s |" in text
    assert "hash-mod: +40% max task time, +60% job time" in text


def test_main_writes_the_file_and_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, summary = tmp_path / "bench.json", tmp_path / "R.md", tmp_path / "summary"
    source.write_text(json.dumps(REPORT), encoding="utf-8")
    summary.write_text("earlier\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main([str(source), str(target)]) == 0
    assert target.read_text(encoding="utf-8") == render(REPORT)
    assert summary.read_text(encoding="utf-8") == "earlier\n" + render(REPORT)


def test_main_without_a_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "bench.json", tmp_path / "R.md"
    source.write_text(json.dumps(REPORT), encoding="utf-8")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main([str(source), str(target)]) == 0
    assert target.read_text(encoding="utf-8") == render(REPORT)
