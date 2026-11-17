"""Command-line entry point.

Only ``--version`` exists for now; the plan, verify and stats commands come
later.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from skewsplit import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the ``skewsplit`` command."""
    parser = argparse.ArgumentParser(
        prog="skewsplit",
        description="Plan balanced JDBC partition predicates for Spark.",
    )
    parser.add_argument("--version", action="version", version=f"skewsplit {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    build_parser().parse_args(argv)
    return 0
