"""Decide which CI jobs run, and which skips ci-gate may accept.

Usage: python scripts/ci_select.py --event EVENT --groups JSON [--labels JSON] [--full]

Writes ``<job>=true|false`` lines and ``allowed-skips=<jobs>`` to the file in
``$GITHUB_OUTPUT`` (or to stdout). ``--groups`` is the ``changes`` output of
dorny/paths-filter: the names of the groups in .github/filters.yml that
matched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# A change to any of these runs every job.
SHARED = frozenset({"spec", "ci", "build"})
PYTHON = frozenset({"py-core", "py-adapters", "cli"})
GROUPS = SHARED | PYTHON | {"rust", "bench", "docs", "repo"}

# The groups that select each job, besides the shared ones. `lint` always runs,
# and lints the Rust crate too, and `pr-size` runs on every pull request. The
# benchmark helpers in bench/ are unit-tested in py-unit; the benchmark itself
# runs in bench.yml. `packaging` builds the Python package and the crate, and
# `conformance` runs the golden fixtures against both.
JOBS: dict[str, frozenset[str]] = {
    "py-unit": PYTHON | {"bench"},
    "py-lowest": PYTHON,
    "py-integration": PYTHON,
    "spark-e2e": PYTHON,
    "coverage": PYTHON | {"bench"},
    "packaging": PYTHON | {"rust"},
    "docs": PYTHON | {"docs"},
    "conformance": PYTHON | {"rust"},
    "core-bench": frozenset({"py-core"}),
    "rust": frozenset({"rust"}),
}
# Jobs outside ci-gate: they report and never block, so they are not skips the
# gate needs to accept.
NOT_GATED = frozenset({"core-bench"})
ALWAYS = ("lint",)
PR_ONLY = ("pr-size",)
FULL_LABEL = "ci:full"


def select(event: str, groups: set[str], labels: set[str], *, full: bool) -> dict[str, bool]:
    """Return, for every job, whether it runs."""
    unknown = groups - GROUPS
    if unknown:
        msg = f"unknown filter groups: {', '.join(sorted(unknown))}"
        raise ValueError(msg)
    is_pr = event == "pull_request"
    run_all = full or not is_pr or FULL_LABEL in labels or bool(groups & SHARED)
    selected = dict.fromkeys(ALWAYS, True)
    selected.update(dict.fromkeys(PR_ONLY, is_pr))
    selected.update({job: run_all or bool(groups & trigger) for job, trigger in JOBS.items()})
    return selected


def render(selected: dict[str, bool]) -> str:
    """Format the selection as GitHub Actions step outputs."""
    lines = [f"{job}={str(run).lower()}" for job, run in selected.items()]
    skips = ",".join(job for job, run in selected.items() if not run and job not in NOT_GATED)
    lines.append(f"allowed-skips={skips}")
    return "\n".join(lines) + "\n"


def _string_list(option: str, text: str) -> set[str]:
    """Parse a JSON list of strings, or fail with a message naming the option."""
    value = json.loads(text)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{option} must be a JSON list of strings, got {text!r}"
        raise TypeError(msg)
    return set(value)


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments and write the outputs."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", required=True)
    parser.add_argument("--groups", default="[]", help="JSON list of matched groups")
    parser.add_argument("--labels", default="[]", help="JSON list of pull request labels")
    parser.add_argument("--full", action="store_true", help="run every job")
    args = parser.parse_args(argv)
    groups = _string_list("--groups", args.groups)
    labels = _string_list("--labels", args.labels)
    selected = select(args.event, groups, labels, full=args.full)
    text = render(selected)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
