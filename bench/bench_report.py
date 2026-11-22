"""Turn the results of bench/run.py into the results page, the README headline and the chart.

Usage: python bench/bench_report.py RESULTS_JSON EVENT_LOG_DIR OUTPUT_DIR

Writes OUTPUT_DIR/RESULTS.md, OUTPUT_DIR/headline.md for the top of the
README, and OUTPUT_DIR/tasks.svg, each task's duration from Spark's event
log. Standard library only. Appends the results to ``$GITHUB_STEP_SUMMARY``
when it is set.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from html import escape
from pathlib import Path
from typing import Any

# The method the headline is about, and the ones it is measured against.
SKEWSPLIT = "skewsplit statistics"
BASELINES = ("default bounds", "hash-mod")
# One colour per method in the chart, in the order of bench_methods.METHODS.
COLOURS = {
    "default bounds": "#8a8a8a",
    "hash-mod": "#c2410c",
    "skewsplit statistics": "#1d4ed8",
    "skewsplit sample": "#0e7490",
}
CHART_WIDTH = 760
LABEL_WIDTH = 170
ROW_HEIGHT = 54
PANEL_GAP = 40


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


def methods(report: dict[str, Any]) -> list[str]:
    """The methods in the order they ran."""
    return list(dict.fromkeys(result["method"] for result in report["results"]))


def runs_of(report: dict[str, Any], column: str, method: str) -> list[dict[str, Any]]:
    """The runs of one method on one column."""
    return [r for r in report["results"] if r["column"] == column and r["method"] == method]


def summaries(report: dict[str, Any], column: str) -> dict[str, dict[str, Any]]:
    """The summary of each method on one column."""
    return {m: summarise(runs_of(report, column, m), report["rows"]) for m in methods(report)}


def setup(report: dict[str, Any]) -> list[str]:
    """What ran where, and what makes it reproducible, as Markdown list items."""
    env = report["environment"]
    lines = [
        (
            f"- Table `{report['table']}`: {report['rows']:,} rows from"
            " `bench/skewed_table.sql`, whose seed (`setseed(0.42)`) makes the data the"
            " same on every run, read in full into Spark's noop sink."
        ),
        (
            f"- {report['partitions']} partitions. Each method ran {report['runs']} times,"
            " interleaved with the others; the times are medians. The sample strategy"
            f" uses `--seed {report['sample_seed']}`. `ANALYZE` reads a random sample of"
            " its own, so the statistics strategy's plan can differ a little between runs."
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
    if report.get("run_url"):
        lines.append(f"- From [this run of the Bench workflow]({report['run_url']}).")
    return lines


def render(report: dict[str, Any]) -> str:
    """The results page as Markdown."""
    lines = [
        "# Benchmark results",
        "",
        "Written by `make bench` from a run of `.github/workflows/bench.yml`. See",
        "[README.md](README.md) for what each method does.",
        "",
        *setup(report),
        "",
        "![Each task's duration, by method](tasks.svg)",
    ]
    for column, pg_type in report["columns"].items():
        stats = summaries(report, column)
        lines += [
            "",
            f"## `{column}` ({pg_type})",
            "",
            (
                "| Method | Tasks | Rows per task (min / median / max) | Largest task |"
                " Planning | Max task time | Job time |"
            ),
            (
                "|--------|------:|-----------------------------------:|-------------:|"
                "---------:|--------------:|---------:|"
            ),
        ]
        lines += [
            f"| {method} | {s['tasks']} | {s['rows'][0]:,} / {s['rows'][1]:,} / {s['rows'][2]:,}"
            f" | {s['share']:.0%} | {seconds(s['plan'])} | {seconds(s['max_task'])}"
            f" | {seconds(s['job'])} |"
            for method, s in stats.items()
        ]
        ours = stats[SKEWSPLIT]
        lines += [""]
        lines += [
            f"{SKEWSPLIT} against {name}: {gain(stats[name]['max_task'], ours['max_task'])}"
            f" max task time, {gain(stats[name]['job'], ours['job'])} job time"
            " (positive is faster)."
            for name in BASELINES
        ]
    return "\n".join(lines) + "\n"


def headline(report: dict[str, Any]) -> str:
    """The README's headline: the largest task and the slowest task of each method."""
    lines = [
        (
            f"Reading a skewed table of {report['rows']:,} rows from PostgreSQL into Spark,"
            f" in {report['partitions']} partitions:"
        ),
        "",
        "| Column | Method | Largest task | Slowest task | Whole read |",
        "|--------|--------|-------------:|-------------:|-----------:|",
    ]
    for column in report["columns"]:
        for method, s in summaries(report, column).items():
            lines.append(
                f"| `{column}` | {method} | {s['share']:.0%} of the rows"
                f" | {seconds(s['max_task'])} | {seconds(s['job'])} |"
            )
    lines += ["", "![Each task's duration, by method](bench/tasks.svg)", ""]
    run = report.get("run_url")
    source = f"[this run of the Bench workflow]({run})" if run else "the Bench workflow"
    lines.append(
        f"Measured by {source}; the setup and every number are in"
        " [bench/RESULTS.md](bench/RESULTS.md)."
    )
    return "\n".join(lines) + "\n"


def task_durations(event_log: Path, groups: set[str]) -> dict[str, list[int]]:
    """Each successful task's duration in milliseconds, by job group, in task order.

    Reads Spark's JSON event log: a job's group is in the properties of its
    SparkListenerJobStart event, and each task's launch and finish times are
    in its SparkListenerTaskEnd event.
    """
    stage_group: dict[int, str] = {}
    tasks: dict[str, list[tuple[int, int]]] = {group: [] for group in groups}
    with event_log.open(encoding="utf-8") as lines:
        for line in lines:
            event = json.loads(line)
            kind = event.get("Event")
            if kind == "SparkListenerJobStart":
                group = (event.get("Properties") or {}).get("spark.jobGroup.id")
                if group in groups:
                    stage_group.update(dict.fromkeys(event["Stage IDs"], group))
            elif kind == "SparkListenerTaskEnd" and event["Stage ID"] in stage_group:
                info = event["Task Info"]
                if event["Task End Reason"]["Reason"] == "Success":
                    duration = info["Finish Time"] - info["Launch Time"]
                    tasks[stage_group[event["Stage ID"]]].append((info["Index"], duration))
    return {group: [ms for _, ms in sorted(found)] for group, found in tasks.items()}


def median_run(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The run whose slowest task is the median one."""
    ordered = sorted(runs, key=lambda run: max(task["ms"] for task in run["tasks"]))
    return ordered[len(ordered) // 2]


def chart(report: dict[str, Any], durations: dict[str, list[int]]) -> str:
    """An SVG of each task's duration: a panel per column, a row of bars per method.

    Each method shows its median run, by slowest task. Bars in a panel share
    one scale, so a tall bar is a slow partition.
    """
    names = methods(report)
    width = CHART_WIDTH
    plot = width - LABEL_WIDTH - 70
    panel = 30 + ROW_HEIGHT * len(names)
    height = 20 + (panel + PANEL_GAP) * len(report["columns"])
    out = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"'
            f' viewBox="0 0 {width} {height}" font-family="system-ui, sans-serif" font-size="13">'
        ),
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    top = 20
    for column in report["columns"]:
        chosen = {m: median_run(runs_of(report, column, m)) for m in names}
        bars = {m: durations[chosen[m]["group"]] for m in names}
        longest = max(max(found) for found in bars.values()) or 1
        out.append(
            f'<text x="10" y="{top + 12}" font-weight="600" fill="#111111">'
            f"{escape(column)}: each task's duration</text>"
        )
        for row, method in enumerate(names):
            base = top + 30 + ROW_HEIGHT * (row + 1) - 8
            found = bars[method]
            step = plot / max(len(found), 1)
            out.append(f'<text x="10" y="{base - 4}" fill="#111111">{escape(method)}</text>')
            for i, ms in enumerate(found):
                tall = max(1.0, (ROW_HEIGHT - 14) * ms / longest)
                out.append(
                    f'<rect x="{LABEL_WIDTH + i * step:.1f}" y="{base - tall:.1f}"'
                    f' width="{max(step - 2, 1):.1f}" height="{tall:.1f}"'
                    f' fill="{COLOURS.get(method, "#555555")}"/>'
                )
            out.append(
                f'<text x="{LABEL_WIDTH + plot + 8}" y="{base - 4}" fill="#333333">'
                f"{max(found) / 1000:.1f} s</text>"
            )
            out.append(
                f'<line x1="{LABEL_WIDTH}" y1="{base}" x2="{LABEL_WIDTH + plot}" y2="{base}"'
                ' stroke="#cccccc"/>'
            )
        top += panel + PANEL_GAP
    out.append("</svg>")
    return "\n".join(out) + "\n"


def event_log_of(report: dict[str, Any], directory: Path) -> Path:
    """The event log of the benchmark's Spark application."""
    path: Path = directory / str(report["event_log"])
    if not path.is_file():
        msg = f"no event log {path}; Spark writes it on stop()"
        raise FileNotFoundError(msg)
    return path


def check(report: dict[str, Any], durations: dict[str, list[int]]) -> None:
    """Every run has as many tasks in the event log as Spark's status API reported."""
    for result in report["results"]:
        found = len(durations[result["group"]])
        if found != len(result["tasks"]):
            msg = (
                f"the event log has {found} tasks of {result['group']},"
                f" the status API {len(result['tasks'])}"
            )
            raise ValueError(msg)


def main(argv: list[str] | None = None) -> int:
    """Write the results page, the headline and the chart, and the job summary."""
    args = sys.argv[1:] if argv is None else argv
    source, events, target = Path(args[0]), Path(args[1]), Path(args[2])
    report = json.loads(source.read_text(encoding="utf-8"))
    groups = {result["group"] for result in report["results"]}
    durations = task_durations(event_log_of(report, events), groups)
    check(report, durations)
    target.mkdir(parents=True, exist_ok=True)
    text = render(report)
    (target / "RESULTS.md").write_text(text, encoding="utf-8")
    (target / "headline.md").write_text(headline(report), encoding="utf-8")
    (target / "tasks.svg").write_text(chart(report, durations), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(text.replace("](tasks.svg)", "](tasks.svg) (in the artifact)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
