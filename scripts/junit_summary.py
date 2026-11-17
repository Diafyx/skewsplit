"""Summarise JUnit XML reports as Markdown for the GitHub job summary.

Usage: python scripts/junit_summary.py TITLE REPORT...

Appends to the file in ``$GITHUB_STEP_SUMMARY`` when it is set, and prints
the summary either way. Always exits 0: the test step itself fails the job.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

MAX_LISTED = 50


@dataclass
class Totals:
    """Counts across every report."""

    tests: int = 0
    failed: int = 0
    skipped: int = 0
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)


def collect(paths: list[Path]) -> Totals:
    """Add up the test cases in the given reports."""
    totals = Totals()
    for path in paths:
        root = ET.parse(path).getroot()  # noqa: S314 -- see the import above
        for case in root.iter("testcase"):
            totals.tests += 1
            totals.seconds += float(case.get("time", "0") or 0)
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            if problem is not None:
                totals.failed += 1
                message = (problem.get("message") or "").splitlines()
                first = f": {message[0]}" if message else ""
                totals.failures.append(f"{case.get('classname')}::{case.get('name')}{first}")
            elif case.find("skipped") is not None:
                totals.skipped += 1
    return totals


def render(title: str, totals: Totals) -> str:
    """Format the totals as Markdown."""
    passed = totals.tests - totals.failed - totals.skipped
    # Emoji shortcodes keep the output ASCII, so printing it works on any
    # console (Windows uses cp1252 before Python 3.15); GitHub renders them.
    mark = ":x:" if totals.failed else ":white_check_mark:"
    lines = [
        f"### {mark} {title}",
        "",
        "| Tests | Passed | Failed | Skipped | Time |",
        "|------:|-------:|-------:|--------:|-----:|",
        (
            f"| {totals.tests} | {passed} | {totals.failed} | {totals.skipped}"
            f" | {totals.seconds:.1f} s |"
        ),
    ]
    if totals.failures:
        lines += ["", "Failed:", ""]
        lines += [f"- `{name}`" for name in totals.failures[:MAX_LISTED]]
        hidden = len(totals.failures) - MAX_LISTED
        if hidden > 0:
            lines.append(f"- and {hidden} more")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Summarise the reports named on the command line."""
    args = sys.argv[1:] if argv is None else argv
    title, reports = args[0], [Path(p) for p in args[1:] if Path(p).is_file()]
    text = render(title, collect(reports))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
