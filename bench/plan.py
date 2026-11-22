"""Plan skewsplit's predicates for the benchmark table, one JSON plan per column and strategy.

Usage: python bench/plan.py --partitions N DIRECTORY

Runs in the project environment, which has skewsplit and psycopg, before the
benchmark starts Spark. It connects with the libpq variables PGHOST, PGPORT,
PGDATABASE and PGUSER, as `make bench` sets them, and writes what
``skewsplit plan --format json`` would.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import bench_methods as m
from skewsplit import postgres


def main(argv: list[str] | None = None) -> int:
    """Write the plans."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--partitions", type=int, required=True)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    args.directory.mkdir(parents=True, exist_ok=True)
    for column in m.COLUMNS:
        for strategy in m.STRATEGIES:
            seed = m.SAMPLE_SEED if strategy == "sample" else None
            # An empty connection string uses the PG* environment variables.
            result = postgres.plan(
                "", m.TABLE, column, args.partitions, strategy=strategy, seed=seed
            )
            path = m.plan_path(args.directory, column, strategy)
            path.write_text(result.to_json() + "\n", encoding="utf-8")
            print(f"{column}, {strategy}: {result.report.splitlines()[0]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
