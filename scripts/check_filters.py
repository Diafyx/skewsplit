"""Fail if a tracked file matches no group in .github/filters.yml.

Usage: python scripts/check_filters.py [FILTERS_FILE]

A file that matches no group would never select a CI job, so a new folder
could slip past CI unnoticed. This guard runs in the lint job.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

DEFAULT_FILTERS = Path(".github/filters.yml")
_GROUP = re.compile(r"([A-Za-z0-9_-]+):")
_ITEM = re.compile(r"\s+-\s+(?:'([^']*)'|(\S+))")


def parse_filters(text: str) -> dict[str, list[str]]:
    """Parse the subset of YAML used by filters.yml: groups of quoted globs.

    Anything else (anchors, inline lists, nesting) raises ``ValueError``, so
    the file cannot drift into syntax this script would read differently from
    dorny/paths-filter.
    """
    groups: dict[str, list[str]] = {}
    current: list[str] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        group = _GROUP.fullmatch(line)
        item = _ITEM.fullmatch(line)
        if group:
            current = groups.setdefault(group.group(1), [])
        elif item and current is not None:
            current.append(item.group(1) or item.group(2))
        else:
            msg = f"line {number}: unsupported syntax: {raw!r}"
            raise ValueError(msg)
    empty = [name for name, patterns in groups.items() if not patterns]
    if empty:
        msg = f"groups without patterns: {', '.join(empty)}"
        raise ValueError(msg)
    return groups


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a picomatch glob (``*``, ``**``, ``?`` only) to a regex."""
    if any(char in pattern for char in "[]{}!"):
        msg = f"unsupported glob syntax in {pattern!r}"
        raise ValueError(msg)
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def unmatched(files: list[str], groups: dict[str, list[str]]) -> list[str]:
    """Return the files that match no pattern of any group."""
    regexes = [glob_to_regex(p) for patterns in groups.values() for p in patterns]
    return [f for f in files if not any(r.fullmatch(f) for r in regexes)]


def tracked_files() -> list[str]:
    """Return every file tracked by git, with forward slashes."""
    out = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return [name for name in out.split("\0") if name]


def main(argv: list[str] | None = None) -> int:
    """Check the tracked files against the filters file."""
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT_FILTERS
    missing = unmatched(tracked_files(), parse_filters(path.read_text(encoding="utf-8")))
    for name in missing:
        print(f"check-filters: {name} matches no group in {path}", file=sys.stderr)
    if missing:
        return 1
    print("check-filters: every tracked file matches a group")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
