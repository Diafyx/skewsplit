"""Compare skewsplit's max task time in a benchmark run with the baseline.

Usage: python bench/bench_compare.py BASELINE_JSON RESULTS_JSON BODY_MD
       python bench/bench_compare.py --baseline RESULTS_JSON BASELINE_JSON

Shared runners differ in speed from one run to the next, so seconds from two
runs do not compare. Each skewsplit method's median max task time is taken as
a share of REFERENCE's in the same run, a read that does not use skewsplit,
and that share is compared with the baseline's.

The first form writes BODY_MD, the text of the tracking issue, and sets
``changed=true`` in ``$GITHUB_OUTPUT`` when a share moved by more than
THRESHOLD percent, either way. The second writes the baseline from a run: the
README's headline run. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from bench_report import summarise

# A change bigger than this, in percent, opens the tracking issue.
THRESHOLD = 10
SKEWSPLIT_METHODS = ("skewsplit statistics", "skewsplit sample")
# The read that skewsplit's times are a share of: Spark's own bounds.
REFERENCE = "default bounds"


def max_tasks(report: dict[str, Any]) -> dict[str, dict[str, float]]:
    """The median max task time, in seconds, of REFERENCE and skewsplit on each column."""
    found: dict[str, dict[str, float]] = {}
    for column in report["columns"]:
        for method in (REFERENCE, *SKEWSPLIT_METHODS):
            runs = [r for r in report["results"] if r["column"] == column and r["method"] == method]
            if runs:
                found.setdefault(column, {})[method] = summarise(runs, report["rows"])["max_task"]
    return found


def baseline(report: dict[str, Any]) -> dict[str, Any]:
    """The baseline that later runs are compared with."""
    return {
        "run_url": report.get("run_url"),
        "rows": report["rows"],
        "partitions": report["partitions"],
        "max_task_seconds": max_tasks(report),
    }


def compare(base: dict[str, Any], report: dict[str, Any]) -> tuple[bool, str]:
    """Whether a share moved by more than THRESHOLD percent, and the report as Markdown."""
    lines = [
        "| Column | Method | Baseline | This run | Change |",
        "|--------|--------|---------:|---------:|-------:|",
    ]
    changed = False
    now = max_tasks(report)
    for column, methods in base["max_task_seconds"].items():
        reference = methods[REFERENCE]
        current = now.get(column, {}).get(REFERENCE)
        shown = "missing" if current is None else f"{current:.1f} s"
        lines.append(f"| `{column}` | {REFERENCE} | {reference:.1f} s | {shown} | |")
        for method, before in methods.items():
            if method == REFERENCE:
                continue
            after = now.get(column, {}).get(method)
            if after is None or current is None:
                changed = True
                lines.append(f"| `{column}` | {method} | {before:.1f} s | missing | |")
                continue
            share_before, share_after = before / reference, after / current
            # The times are whole milliseconds; rounding keeps a share of 0.55 against 0.5 at 10%.
            change = round((share_after - share_before) / share_before * 100, 6)
            moved = abs(change) > THRESHOLD
            changed = changed or moved
            mark = " **over the threshold**" if moved else ""
            lines.append(
                f"| `{column}` | {method} | {before:.1f} s, {share_before:.1%}"
                f" | {after:.1f} s, {share_after:.1%} | {change:+.0f}%{mark} |"
            )
    same = (base["rows"], base["partitions"]) == (report["rows"], report["partitions"])
    intro = [
        (
            f"skewsplit's median max task time in the nightly benchmark, as a share of"
            f" {REFERENCE}' in the same run, against the baseline from"
            f" {base['run_url'] or 'the committed baseline'}. Runners differ in speed, so the"
            f" share is compared rather than the seconds. A change of the share by more than"
            f" {THRESHOLD}% either way opens this issue."
        ),
        "",
    ]
    if not same:
        intro += [
            (
                f"The baseline read {base['rows']:,} rows in {base['partitions']} partitions,"
                f" this run {report['rows']:,} in {report['partitions']}, so the times do not"
                " compare."
            ),
            "",
        ]
        changed = True
    if report.get("run_url"):
        lines += ["", f"This run, with `bench-results` as its artifact: {report['run_url']}"]
    return changed, "\n".join(intro + lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Compare, or write the baseline."""
    args = sys.argv[1:] if argv is None else argv
    if args[0] == "--baseline":
        report = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        text = json.dumps(baseline(report), indent=2) + "\n"
        Path(args[2]).write_text(text, encoding="utf-8")
        return 0
    base = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    report = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    changed, body = compare(base, report)
    Path(args[2]).write_text(body, encoding="utf-8")
    print(body)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
