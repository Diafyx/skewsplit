from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any

import pytest

from bench_report import (
    chart,
    check,
    event_log_of,
    gain,
    headline,
    main,
    median_run,
    render,
    summarise,
    task_durations,
)

if TYPE_CHECKING:
    from pathlib import Path


def run(
    method: str, seconds: float, tasks: list[tuple[int, int]], number: int = 1
) -> dict[str, Any]:
    return {
        "column": "customer_id",
        "method": method,
        "run": number,
        "group": f"customer_id/{method}/{number}",
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
    "sample_seed": 1,
    "run_url": "https://github.com/o/r/actions/runs/7",
    "event_log": "local-1",
    "results": [
        run("default bounds", 9.0, [(8000, 900), (1000, 100)]),
        run("hash-mod", 10.0, [(5000, 10), (5000, 495), (5000, 495)]),
        run("skewsplit statistics", 4.0, [(100, 10), (2000, 490), (3000, 500)]),
        run("skewsplit sample", 5.0, [(100, 10), (2500, 480), (3500, 510)]),
    ],
}


def events(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The event log Spark would write for REPORT: one job of one stage per run."""
    out: list[dict[str, Any]] = [{"Event": "SparkListenerApplicationStart"}]
    for stage, result in enumerate(report["results"]):
        group = result["group"]
        out.append(
            {
                "Event": "SparkListenerJobStart",
                "Job ID": stage,
                "Stage IDs": [stage],
                "Properties": {"spark.jobGroup.id": group},
            }
        )
        out.extend(
            {
                "Event": "SparkListenerTaskEnd",
                "Stage ID": stage,
                "Task End Reason": {"Reason": "Success"},
                "Task Info": {
                    "Index": task["index"],
                    "Launch Time": 1000,
                    "Finish Time": 1000 + task["ms"],
                },
            }
            # In reverse, so the report has to sort the tasks.
            for task in reversed(result["tasks"])
        )
    # A failed attempt, and a job of another group, which are left out.
    out.append(
        {
            "Event": "SparkListenerTaskEnd",
            "Stage ID": 0,
            "Task End Reason": {"Reason": "ExceptionFailure"},
            "Task Info": {"Index": 0, "Launch Time": 0, "Finish Time": 99999},
        }
    )
    out.append(
        {"Event": "SparkListenerJobStart", "Job ID": 99, "Stage IDs": [99], "Properties": {}}
    )
    out.append(
        {
            "Event": "SparkListenerTaskEnd",
            "Stage ID": 99,
            "Task End Reason": {"Reason": "Success"},
            "Task Info": {"Index": 0, "Launch Time": 0, "Finish Time": 5},
        }
    )
    return out


def write_events(directory: Path, report: dict[str, Any]) -> Path:
    path: Path = directory / str(report["event_log"])
    path.write_text("\n".join(json.dumps(e) for e in events(report)) + "\n", encoding="utf-8")
    return path


def test_gain_is_positive_when_faster() -> None:
    assert gain(10.0, 7.5) == "+25%"
    assert gain(10.0, 12.0) == "-20%"


def test_summarise_takes_medians_over_runs() -> None:
    runs = [
        run("m", 3.0, [(1000, 5), (4000, 15)]),
        run("m", 1.0, [(2000, 5), (2000, 15)]),
        run("m", 2.0, [(9000, 5), (1000, 15)]),
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
    assert "- Table `bench_orders`: 1,000 rows from `bench/skewed_table.sql`" in text
    assert "whose seed (`setseed(0.42)`) makes the data the same on every run" in text
    assert "The sample strategy uses `--seed 1`." in text
    assert "(ubuntu24 20261116.1, 4 CPUs, 16 GB)" in text
    assert "- From [this run of the Bench workflow](https://github.com/o/r/actions/runs/7)." in text
    assert "![Each task's duration, by method](tasks.svg)" in text
    assert "## `customer_id` (bigint)" in text
    assert "| default bounds | 2 | 100 / 500 / 900 | 90% | 0.5 s | 8.0 s | 9.0 s |" in text
    assert "| hash-mod | 3 | 10 / 495 / 495 | 50% | 0.5 s | 5.0 s | 10.0 s |" in text
    assert "| skewsplit statistics | 3 | 10 / 490 / 500 | 50% | 0.5 s | 3.0 s | 4.0 s |" in text
    assert "| skewsplit sample | 3 | 10 / 480 / 510 | 51% | 0.5 s | 3.5 s | 5.0 s |" in text
    assert "skewsplit statistics against default bounds: +62% max task time, +56% job time" in text
    assert "skewsplit statistics against hash-mod: +40% max task time, +60% job time" in text


def test_render_names_the_processor() -> None:
    report = {**REPORT, "environment": {**REPORT["environment"], "cpu": "AMD EPYC 7763"}}
    assert "(ubuntu24 20261116.1, 4 CPUs, AMD EPYC 7763, 16 GB)" in render(report)


def test_render_without_a_run() -> None:
    assert "From [this run" not in render({**REPORT, "run_url": None})


def test_headline_links_the_run_and_the_chart() -> None:
    text = headline(REPORT)
    assert text.startswith(
        "Reading a skewed table of 1,000 rows from PostgreSQL into Spark, in 2 partitions:\n"
    )
    assert "| `customer_id` | default bounds | 90% of the rows | 8.0 s | 9.0 s |" in text
    assert "| `customer_id` | skewsplit statistics | 50% of the rows | 3.0 s | 4.0 s |" in text
    assert "![Each task's duration, by method](bench/tasks.svg)" in text
    assert text.endswith(
        "Measured by [this run of the Bench workflow](https://github.com/o/r/actions/runs/7);"
        " the setup and every number are in [bench/RESULTS.md](bench/RESULTS.md).\n"
    )
    assert "Measured by the Bench workflow;" in headline({**REPORT, "run_url": None})


def test_task_durations_follow_the_job_groups(tmp_path: Path) -> None:
    path = write_events(tmp_path, REPORT)
    groups = {result["group"] for result in REPORT["results"]}
    found = task_durations(path, groups)
    assert found == {
        "customer_id/default bounds/1": [8000, 1000],
        "customer_id/hash-mod/1": [5000, 5000, 5000],
        "customer_id/skewsplit statistics/1": [100, 2000, 3000],
        "customer_id/skewsplit sample/1": [100, 2500, 3500],
    }
    check(REPORT, found)


def test_check_finds_a_missing_task() -> None:
    durations = {result["group"]: [1] * len(result["tasks"]) for result in REPORT["results"]}
    durations["customer_id/hash-mod/1"] = [1]
    with pytest.raises(ValueError, match="the event log has 1 tasks of customer_id/hash-mod/1"):
        check(REPORT, durations)


def test_median_run_is_the_middle_one_by_slowest_task() -> None:
    runs = [run("m", 1.0, [(ms, 1)], n) for n, ms in enumerate([300, 100, 200])]
    assert median_run(runs)["run"] == 2


def test_chart_draws_a_bar_per_task() -> None:
    durations = {
        result["group"]: [t["ms"] for t in result["tasks"]] for result in REPORT["results"]
    }
    svg = chart(REPORT, durations)
    root = ET.fromstring(svg)  # noqa: S314 -- our own output
    namespace = "{http://www.w3.org/2000/svg}"
    bars = list(root.iter(f"{namespace}rect"))[1:]
    assert len(bars) == 2 + 3 + 3 + 3
    texts = [text.text for text in root.iter(f"{namespace}text")]
    assert "customer_id: each task's duration" in texts
    assert {"default bounds", "hash-mod", "skewsplit statistics", "skewsplit sample"} <= set(texts)
    assert {"8.0 s", "5.0 s", "3.0 s", "3.5 s"} <= set(texts)
    # The slowest task in the panel is the tallest bar.
    tallest = max(bars, key=lambda rect: float(rect.get("height", "0")))
    assert tallest.get("fill") == "#8a8a8a"


def test_event_log_of_needs_the_finished_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no event log"):
        event_log_of(REPORT, tmp_path)
    path = write_events(tmp_path, REPORT)
    assert event_log_of(REPORT, tmp_path) == path


def test_main_writes_the_page_the_headline_and_the_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, summary, out = tmp_path / "bench.json", tmp_path / "summary", tmp_path / "out"
    source.write_text(json.dumps(REPORT), encoding="utf-8")
    write_events(tmp_path, REPORT)
    summary.write_text("earlier\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main([str(source), str(tmp_path), str(out)]) == 0
    assert (out / "RESULTS.md").read_text(encoding="utf-8") == render(REPORT)
    assert (out / "headline.md").read_text(encoding="utf-8") == headline(REPORT)
    assert (out / "tasks.svg").read_text(encoding="utf-8").startswith("<svg ")
    assert summary.read_text(encoding="utf-8") == "earlier\n" + render(REPORT).replace(
        "](tasks.svg)", "](tasks.svg) (in the artifact)"
    )


def test_main_without_a_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, out = tmp_path / "bench.json", tmp_path / "out"
    source.write_text(json.dumps(REPORT), encoding="utf-8")
    write_events(tmp_path, REPORT)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main([str(source), str(tmp_path), str(out)]) == 0
    assert (out / "RESULTS.md").is_file()
