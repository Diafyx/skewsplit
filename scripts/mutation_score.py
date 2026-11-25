"""Check the mutation score of the core or of the crate against the minimum.

Usage: python scripts/mutation_score.py [--tool TOOL] --min PERCENT STATS_JSON...

STATS_JSON is what ``mutmut export-cicd-stats`` writes (``--tool mutmut``, the
default), or the ``outcomes.json`` of cargo-mutants (``--tool cargo-mutants``).
Several files, one for each shard of a run, are added up. The score is the
share of the tested mutants that the tests killed. A mutant mutmut skipped, or
one cargo-mutants found unviable because it does not build, is not tested; a
mutant that timed out counts as not killed. Prints a Markdown summary, also to
$GITHUB_STEP_SUMMARY, writes ``score=`` to $GITHUB_OUTPUT when those are set,
and exits 1 when the score is below the minimum.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple


class Tool(NamedTuple):
    """How a mutation testing tool reports its counts."""

    title: str
    counts: tuple[str, ...]  # in the order the summary lists them
    total: str
    killed: str
    untested: str


MUTMUT = Tool(
    "Mutation testing of the core",
    ("killed", "survived", "timeout", "suspicious", "no_tests", "segfault", "skipped"),
    total="total",
    killed="killed",
    untested="skipped",
)
CARGO_MUTANTS = Tool(
    "Mutation testing of the crate",
    ("caught", "missed", "timeout", "unviable"),
    total="total_mutants",
    killed="caught",
    untested="unviable",
)
TOOLS = {"mutmut": MUTMUT, "cargo-mutants": CARGO_MUTANTS}


def score(stats: dict[str, int], tool: Tool = MUTMUT) -> float | None:
    """Return the percentage of tested mutants killed, or None if none was tested."""
    tested = stats[tool.total] - stats[tool.untested]
    return None if tested <= 0 else 100 * stats[tool.killed] / tested


def summary(stats: dict[str, int], found: float | None, minimum: float, tool: Tool = MUTMUT) -> str:
    """Return the Markdown summary of a run."""
    if found is None:
        shown = "no mutant was tested"
    else:
        shown = f"{found:.1f}% of the tested mutants killed"
    verdict = "passes" if found is not None and found >= minimum else "fails"
    lines = [
        f"## {tool.title}",
        "",
        f"Score: {shown} (minimum {minimum:g}%): {verdict}.",
        "",
        "| Mutants | Count |",
        "|---------|-------|",
        *(f"| {name.replace('_', ' ')} | {stats[name]} |" for name in tool.counts),
        f"| total | {stats[tool.total]} |",
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
    parser.add_argument("--tool", choices=sorted(TOOLS), default="mutmut")
    parser.add_argument("--min", type=float, required=True, dest="minimum")
    parser.add_argument("stats", type=Path, nargs="+")
    args = parser.parse_args(argv)
    tool = TOOLS[args.tool]
    stats = dict.fromkeys((*tool.counts, tool.total), 0)
    for path in args.stats:
        data = json.loads(path.read_text(encoding="utf-8"))
        for name in stats:
            stats[name] += int(data[name])
    found = score(stats, tool)
    text = summary(stats, found, args.minimum, tool)
    print(text, end="")
    append("GITHUB_STEP_SUMMARY", text)
    append("GITHUB_OUTPUT", f"score={'' if found is None else f'{found:.1f}'}\n")
    return 0 if found is not None and found >= args.minimum else 1


if __name__ == "__main__":
    sys.exit(main())
