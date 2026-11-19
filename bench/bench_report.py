"""Turn the JSON from bench/run.py into bench/RESULTS.md and the job summary.

Usage: python bench/bench_report.py RESULTS_JSON OUTPUT_MD

Standard library only. Appends to ``$GITHUB_STEP_SUMMARY`` when it is set.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

# The kill criterion of the spike: ranges should beat hash-mod by about this much.
THRESHOLD = 20


def seconds(value: float) -> str:
    """Seconds with one decimal."""
    return f"{value:.1f} s"


def gain(baseline: float, value: float) -> str:
    """How much lower ``value`` is than ``baseline``, as a signed percentage."""
    return f"{(baseline - value) / baseline * 100:+.0f}%"


def summarise(runs: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """Medians over the runs of one method on one column."""
    rows = [task["rows"] for task in runs[0]["tasks"]]
    return {
        "tasks": len(rows),
        "rows": (min(rows), int(statistics.median(rows)), max(rows)),
        "share": max(rows) / total,
        "plan": statistics.median(run["plan_seconds"] for run in runs),
        "max_task": statistics.median(max(t["ms"] for t in run["tasks"]) / 1000 for run in runs),
        "job": statistics.median(run["seconds"] for run in runs),
    }


def render(report: dict[str, Any]) -> str:
    """The results as Markdown."""
    env = report["environment"]
    total = report["rows"]
    lines = [
        "# Benchmark results",
        "",
        "Written by `make bench` from a run of `.github/workflows/bench.yml`. See",
        "[README.md](README.md) for what each method does.",
        "",
        f"- Table `{report['table']}`: {total:,} rows, read in full into Spark's noop sink.",
        (
            f"- {report['partitions']} partitions. Each method ran {report['runs']} times,"
            " interleaved with the others; the times are medians."
        ),
        (
            f"- Spark {env['spark']} in local mode with {env['cpus']} cores, Java {env['java']},"
            f" Python {env['python']}."
        ),
        (
            f"- PostgreSQL {env['postgres']} in a service container on the same runner"
            f" ({env['runner'] or 'unknown image'}, {env['cpus']} CPUs, {env['memory_gb']} GB)."
        ),
    ]
    for column, pg_type in report["columns"].items():
        stats = {}
        for method in dict.fromkeys(r["method"] for r in report["results"]):
            runs = [r for r in report["results"] if r["column"] == column and r["method"] == method]
            stats[method] = summarise(runs, total)
        lines += [
            "",
            f"## `{column}` ({pg_type})",
            "",
            (
                "| Method | Tasks | Rows per task (min / median / max) | Largest task |"
                " Planning query | Max task time | Job time |"
            ),
            (
                "|--------|------:|-----------------------------------:|-------------:|"
                "---------------:|--------------:|---------:|"
            ),
        ]
        lines += [
            f"| {method} | {s['tasks']} | {s['rows'][0]:,} / {s['rows'][1]:,} / {s['rows'][2]:,}"
            f" | {s['share']:.0%} | {seconds(s['plan'])} | {seconds(s['max_task'])}"
            f" | {seconds(s['job'])} |"
            for method, s in stats.items()
        ]
        hashed, ranges = stats["hash-mod"], stats["quantile ranges"]
        lines += [
            "",
            (
                f"Quantile ranges against hash-mod: {gain(hashed['max_task'], ranges['max_task'])}"
                f" max task time, {gain(hashed['job'], ranges['job'])} job time"
                f" (positive is faster; the kill criterion is about {THRESHOLD}%)."
            ),
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write the Markdown file, and the job summary when there is one."""
    args = sys.argv[1:] if argv is None else argv
    source, target = Path(args[0]), Path(args[1])
    text = render(json.loads(source.read_text(encoding="utf-8")))
    target.write_text(text, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
