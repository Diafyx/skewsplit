"""Open a tracking issue, or comment on it if it is already open.

Usage: python scripts/tracking_issue.py --title TITLE --label LABEL...
           --body-file FILE [--dry-run]

The open issue with exactly this title is the tracking issue: the body becomes
a new comment on it. Without one, a new issue gets the title, the body and the
labels. With ``--dry-run``, the lookup still runs and the writes are printed
instead of made, which tests the path without touching any issue.

Runs ``gh``, which reads GH_TOKEN and GH_REPO, as in a workflow step.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

Runner = Callable[[list[str]], str]
# More open issues than the repository should ever have.
LIMIT = 1000


def run_gh(args: list[str]) -> str:
    """Run gh and return what it prints; raise if it fails."""
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        msg = f"gh {shlex.join(args)} failed: {done.stderr.strip()}"
        raise RuntimeError(msg)
    return done.stdout


def find(title: str, gh: Runner) -> int | None:
    """Return the number of the open issue titled exactly title, if there is one.

    It lists the open issues rather than searching: the search index can lag,
    and a label may have changed since the issue was opened.
    """
    listed = ["issue", "list", "--state", "open", "--limit", str(LIMIT)]
    found = gh([*listed, "--json", "number,title"])
    numbers = [issue["number"] for issue in json.loads(found) if issue["title"] == title]
    return min(numbers) if numbers else None


def track(title: str, labels: list[str], body_file: Path, *, dry_run: bool, gh: Runner) -> str:
    """Comment on the tracking issue, or open it; return what was done."""
    number = find(title, gh)
    if number is None:
        write = ["issue", "create", "--title", title, "--body-file", str(body_file)]
        write += [part for label in labels for part in ("--label", label)]
        done = "opened a new issue"
    else:
        write = ["issue", "comment", str(number), "--body-file", str(body_file)]
        done = f"commented on #{number}"
    if dry_run:
        return f"dry run: would have {done}: gh {shlex.join(write)}"
    gh(write)
    return done


def main(argv: list[str] | None = None) -> int:
    """Open or update the tracking issue and print what was done."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--title", required=True)
    parser.add_argument("--label", action="append", required=True, dest="labels")
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.body_file.read_text(encoding="utf-8").strip():
        parser.error(f"{args.body_file} is empty")
    print(track(args.title, args.labels, args.body_file, dry_run=args.dry_run, gh=run_gh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
