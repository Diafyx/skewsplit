"""Check the mutation score of the core against the minimum.

Usage: python scripts/mutation_score.py --min PERCENT STATS_JSON

STATS_JSON is what ``mutmut export-cicd-stats`` writes. The score is the share
of the tested mutants that the tests killed; a skipped mutant is not tested,
and a mutant that timed out counts as not killed. Prints a Markdown summary,
also to $GITHUB_STEP_SUMMARY, writes ``score=`` to $GITHUB_OUTPUT when those
are set, and exits 1 when the score is below the minimum.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The counts mutmut reports, in the order the summary lists them.
COUNTS = ("killed", "survived", "timeout", "suspicious", "no_tests", "segfault", "skipped")


def score(stats: dict[str, int]) -> float | None:
    """Return the percentage of tested mutants killed, or None if none was tested."""
    tested = stats["total"] - stats["skipped"]
    return None if tested <= 0 else 100 * stats["killed"] / tested


def summary(stats: dict[str, int], found: float | None, minimum: float) -> str:
    """Return the Markdown summary of a run."""
    if found is None:
        shown = "no mutant was tested"
    else:
        shown = f"{found:.1f}% of the tested mutants killed"
    verdict = "passes" if found is not None and found >= minimum else "fails"
    lines = [
        "## Mutation testing of the core",
        "",
        f"Score: {shown} (minimum {minimum:g}%): {verdict}.",
        "",
        "| Mutants | Count |",
        "|---------|-------|",
        *(f"| {name.replace('_', ' ')} | {stats[name]} |" for name in COUNTS),
        f"| total | {stats['total']} |",
        "",
    ]
    return "\n".join(lines)


def append(variable: str, text: str) -> None:
    """Append text to the file that the environment variable names, if it is set."""
    path = os.environ.get(variable)
    if path:
        with Path(path).open("a", encoding="utf-8") as file:
            file.write(text)


def main(argv: list[str] | None = None) -> int:
    """Report the score; return 1 when it is below the minimum."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min", type=float, required=True, dest="minimum")
    parser.add_argument("stats", type=Path)
    args = parser.parse_args(argv)
    data = json.loads(args.stats.read_text(encoding="utf-8"))
    stats = {name: int(data[name]) for name in (*COUNTS, "total")}
    found = score(stats)
    text = summary(stats, found, args.minimum)
    print(text, end="")
    append("GITHUB_STEP_SUMMARY", text)
    append("GITHUB_OUTPUT", f"score={'' if found is None else f'{found:.1f}'}\n")
    return 0 if found is not None and found >= args.minimum else 1


if __name__ == "__main__":
    sys.exit(main())
