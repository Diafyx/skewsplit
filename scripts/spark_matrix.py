"""Print the legs of ci.yml's spark-e2e job as a GitHub Actions matrix.

Usage: python scripts/spark_matrix.py [--every-python]

Each Spark line runs on its oldest and newest supported Python, with every Java
it supports. With ``--every-python``, as nightly.yml asks for, it also runs on
every Python in between. Prints ``spark-matrix=<json>``, and appends it to
$GITHUB_OUTPUT when that is set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Every Python the package supports, oldest first.
PYTHONS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "3.15")
# Each tested Spark line: the oldest and newest Python its PySpark supports,
# and the Javas it supports. A line also needs its pinned PySpark in
# python/tests/spark/pyspark-<line>/requirements.txt.
LINES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "3.5": ("3.9", "3.11", ("17",)),
    "4.1": ("3.10", "3.14", ("17", "21")),
    "4.2": ("3.10", "3.14", ("17", "21")),
    "4.3": ("3.11", "3.14", ("17", "21")),
}


def legs(*, every_python: bool) -> list[dict[str, str]]:
    """Return one leg for each Spark line, Python and Java to test."""
    found = []
    for spark, (oldest, newest, javas) in LINES.items():
        span = PYTHONS[PYTHONS.index(oldest) : PYTHONS.index(newest) + 1]
        pythons = span if every_python else (oldest, newest)
        found += [
            {"spark": spark, "python": python, "java": java} for python in pythons for java in javas
        ]
    return found


def main(argv: list[str] | None = None) -> int:
    """Print the matrix, and write it to $GITHUB_OUTPUT when that is set."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--every-python", action="store_true")
    args = parser.parse_args(argv)
    matrix = {"include": legs(every_python=args.every_python)}
    line = f"spark-matrix={json.dumps(matrix, separators=(',', ':'))}\n"
    print(line, end="")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as file:
            file.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
