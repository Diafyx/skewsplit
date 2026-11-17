"""Compute a pull request's size label and enforce the size/XXL rule.

Usage: python scripts/pr_size.py BASE HEAD [BODY_FILE]

Counts added plus deleted lines between the merge base of BASE and HEAD,
excluding files marked ``linguist-generated`` in .gitattributes. Prints the
label and writes ``label=<label>`` to ``$GITHUB_OUTPUT`` when it is set. Exits
1 for a size/XXL pull request whose "Why is this PR large?" section is empty.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SIZES = ((100, "size/S"), (300, "size/M"), (700, "size/L"), (1500, "size/XL"))
XXL = "size/XXL"
WHY_HEADING = "### Why is this PR large?"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_EMPTY_ANSWERS = frozenset({"", "n/a", "na", "no", "none"})


def size_label(lines: int) -> str:
    """Return the size label for a number of changed lines."""
    for limit, label in SIZES:
        if lines <= limit:
            return label
    return XXL


def parse_numstat(numstat: str) -> dict[str, int]:
    """Map each path in ``git diff --numstat -z`` output to its changed lines."""
    changed = {}
    for record in numstat.split("\0"):
        if not record:
            continue
        added, deleted, path = record.split("\t", 2)
        # Binary files show "-" for both counts and count as zero lines.
        changed[path] = sum(int(n) for n in (added, deleted) if n != "-")
    return changed


def parse_check_attr(output: str) -> set[str]:
    """Return the paths that ``git check-attr -z`` reports as generated."""
    fields = output.split("\0")
    triples = zip(fields[0::3], fields[1::3], fields[2::3])
    return {path for path, _attr, value in triples if value in {"set", "true"}}


def why_large_filled(body: str) -> bool:
    """Return whether the "Why is this PR large?" section has a real answer."""
    _, found, rest = body.partition(WHY_HEADING)
    if not found:
        return False
    section = re.split(r"^#{1,3} ", rest, maxsplit=1, flags=re.MULTILINE)[0]
    answer = _COMMENT.sub("", section).strip()
    return answer.lower().rstrip(".") not in _EMPTY_ANSWERS


def _git(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], input=stdin, check=True, capture_output=True, text=True
    ).stdout


def changed_lines(base: str, head: str) -> int:
    """Count changed lines between ``base`` and ``head``, minus generated files."""
    changed = parse_numstat(_git("diff", "--numstat", "-z", "--no-renames", f"{base}...{head}"))
    if not changed:
        return 0
    attrs = _git("check-attr", "-z", "--stdin", "linguist-generated", stdin="\0".join(changed))
    generated = parse_check_attr(attrs)
    return sum(lines for path, lines in changed.items() if path not in generated)


def main(argv: list[str] | None = None) -> int:
    """Print the label and apply the size/XXL rule."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", help="the branch the pull request merges into")
    parser.add_argument("head", help="the pull request's head commit")
    parser.add_argument("body", nargs="?", type=Path, help="file with the PR description")
    args = parser.parse_args(argv)
    body = args.body.read_text(encoding="utf-8") if args.body else ""
    lines = changed_lines(args.base, args.head)
    label = size_label(lines)
    print(f"pr-size: {lines} changed lines -> {label}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"label={label}\n")
    if label == XXL and not why_large_filled(body):
        print(f'pr-size: {XXL} needs a filled-in "{WHY_HEADING[4:]}" section', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
