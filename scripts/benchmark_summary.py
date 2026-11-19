"""Summarise a pytest-benchmark JSON report as Markdown for the GitHub job summary.

Usage: python scripts/benchmark_summary.py TITLE REPORT

Appends to the file in ``$GITHUB_STEP_SUMMARY`` when it is set, and prints
the summary either way. Always exits 0: benchmarks report and never gate.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ASCII units, as in junit_summary.py, so printing works on any console.
_UNITS = ((1.0, "s"), (1e-3, "ms"), (1e-6, "us"))


def duration(seconds: float) -> str:
    """Format a duration with three significant digits, in s, ms or us."""
    scale, unit = next((u for u in _UNITS if seconds >= u[0]), _UNITS[-1])
    return f"{seconds / scale:.3g} {unit}"


def render(title: str, report: dict[str, Any] | None) -> str:
    """Format the benchmarks in a report, or say that there is none."""
    lines = [f"### {title}", ""]
    benchmarks = report.get("benchmarks") if report else None
    if not isinstance(benchmarks, list) or not benchmarks:
        return "\n".join([*lines, "No benchmark results were recorded."]) + "\n"
    lines += ["| Benchmark | Median | Min | Rounds |", "|-----------|-------:|----:|-------:|"]
    for bench in benchmarks:
        stats = bench["stats"]
        lines.append(
            f"| `{bench['name']}` | {duration(stats['median'])} | {duration(stats['min'])}"
            f" | {stats['rounds']} |"
        )
    machine = report.get("machine_info") if report else None
    if isinstance(machine, dict):
        cpu = machine.get("cpu", {}).get("brand_raw", "unknown CPU")
        lines += [
            "",
            f"Python {machine.get('python_version')} on {cpu}. Timings inform; they do not gate.",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Summarise the report named on the command line."""
    args = sys.argv[1:] if argv is None else argv
    title, path = args[0], Path(args[1])
    report = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    text = render(title, report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
