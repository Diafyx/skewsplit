"""The ways the benchmark splits a JDBC read, as SQL text for Spark.

Standard library only, so the unit tests run without Spark. skewsplit's own
predicates come from bench/plan.py, which plans them with the package before
Spark starts.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

TABLE = "bench_orders"
# The split columns and their PostgreSQL types.
COLUMNS = {"customer_id": "bigint", "created_at": "timestamp"}
# skewsplit's strategies, each with its predicates planned by bench/plan.py.
STRATEGIES: tuple[Literal["statistics", "sample"], ...] = ("statistics", "sample")
# The REPEATABLE seed of the sample, so every run plans from the same rows.
SAMPLE_SEED = 1
METHODS = ("default bounds", "hash-mod", *(f"skewsplit {s}" for s in STRATEGIES))


def quote(name: str) -> str:
    """Quote a PostgreSQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def bounds_query(column: str) -> str:
    """The lowest and highest value, as a user of ``partitionColumn`` would read them."""
    col, table = quote(column), quote(TABLE)
    return f"SELECT min({col})::text AS lo, max({col})::text AS hi FROM {table}"  # noqa: S608 -- fixed, quoted identifiers


def hash_mod(column: str, partitions: int) -> list[str]:
    """A NULL predicate, then one predicate per hash bucket, as a user would write them by hand.

    Each scans the whole table.
    """
    col = quote(column)
    bucket = f"((hashtextextended({col}::text, 0) % {partitions}) + {partitions}) % {partitions}"
    return [f"{col} IS NULL", *(f"{bucket} = {i}" for i in range(partitions))]


def strategy_of(method: str) -> str | None:
    """The skewsplit strategy a method uses, or ``None`` for the other methods."""
    prefix = "skewsplit "
    return method[len(prefix) :] if method.startswith(prefix) else None


def plan_path(directory: Path, column: str, strategy: str) -> Path:
    """Where bench/plan.py writes the plan of one column and strategy."""
    return directory / f"{column}-{strategy}.json"


def read_plan(path: Path) -> tuple[list[str], float]:
    """The predicates of a plan that ``skewsplit plan --format json`` wrote, and its planning time.

    The planning time is the seconds skewsplit spent finding the column,
    reading the statistics or the sample, and planning.
    """
    plan = json.loads(path.read_text(encoding="utf-8"))
    predicates = [predicate["sql"] for predicate in plan["predicates"]]
    return predicates, plan["source"]["planning_ms"] / 1000
