"""Check the golden fixtures for what their JSON Schema cannot express.

Usage: python scripts/check_fixtures.py [FIXTURES_DIR]

`make check-fixtures` runs this after validating the fixtures against their
schema. It checks each plan against its input and the rules of spec/SPEC.md
that need no planner: row counts, the order of predicates, warnings and the
skew score. Running the fixtures through a planner is each implementation's job.

The array text fixtures in pg-array/ (spec section 12) have no plan; only
their names and spec versions are checked here. For the statistics fixtures
in pg-stats/ (spec section 13) and the sample fixtures in sample/ (section 14),
it also checks the warnings in each result's source against its row counts.
A hash plan in hash/ (section 16) follows from its row counts alone, so each
is checked in full.
"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path("spec/fixtures")
# Fixtures of the other functions of the spec, in subdirectories.
SUBDIRS = ("pg-array", "pg-stats", "sample", "hash")
SPEC = Path("spec/SPEC.md")
_VERSION = re.compile(r"^Spec version: \*\*(\d+\.\d+)\*\*$", re.MULTILINE)
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.json")
# Spec section 8: the order of warning codes in a plan.
WARNING_ORDER = (
    "dominant_value",
    "dominant_value_not_isolated",
    "fewer_partitions",
    "nulls_exceed_share",
    "no_rows",
)
# Spec section 13: more than a tenth of the rows modified is stale.
STALE_DIVISOR = 10


def spec_version(spec_text: str) -> str:
    """Return the version stated in SPEC.md."""
    found = _VERSION.findall(spec_text)
    if len(found) != 1:
        msg = f"expected one 'Spec version: **X.Y**' line in the spec, found {len(found)}"
        raise ValueError(msg)
    return str(found[0])


def check_name(name: str, *, is_error: bool) -> list[str]:
    """Check a fixture's file name: kebab case, and `error-` exactly for errors."""
    problems = []
    if not _NAME.fullmatch(name):
        problems.append("file name must be lowercase words joined by '-', ending in .json")
    if is_error != name.startswith("error-"):
        problems.append("error fixtures, and only they, have names starting with 'error-'")
    return problems


def check_ranges(predicates: list[dict[str, Any]]) -> list[str]:
    """Check that the non-NULL predicates fit together (spec sections 5 and 6).

    The first range is open below and the last open above. Each range ends
    where the next predicate starts: at a split point (`<` then `>=`) or at a
    dominant value (`<` before its `=` predicate and `>` after it).
    """
    rest = predicates[1:]
    problems = []
    if rest[0]["kind"] != "range" or rest[0]["lower"] is not None:
        problems.append("the first non-NULL predicate must be a range open below")
    if rest[-1]["kind"] != "range" or rest[-1]["upper"] is not None:
        problems.append("the last predicate must be a range open above")
    for before, after in zip(rest, rest[1:]):
        if before["kind"] == "value" and after["kind"] == "value":
            problems.append(f"no range between = {before['value']} and = {after['value']}")
        elif before["kind"] == "range" and after["kind"] == "range":
            expected = {"op": ">=", "value": (before["upper"] or {}).get("value")}
            if before["upper"] is None or after["lower"] != expected:
                problems.append(
                    f"range {after['sql']!r} does not start where {before['sql']!r} ends"
                )
        elif before["kind"] == "range":
            if before["upper"] != {"op": "<", "value": after["value"]}:
                problems.append(f"range {before['sql']!r} does not end at = {after['value']}")
        elif after["lower"] != {"op": ">", "value": before["value"]}:
            problems.append(f"range {after['sql']!r} does not start after = {before['value']}")
    return problems


def check_warnings(inp: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """Check the warnings against the plan (spec section 8)."""
    warnings = plan["warnings"]
    rows = plan["rows"]
    n = inp["partitions"]
    codes = [w["code"] for w in warnings]
    problems = []
    if codes != sorted(codes, key=WARNING_ORDER.index):
        problems.append(f"warnings are out of order: {', '.join(codes)}")
    problems += [
        f"{code} is given more than once"
        for code in ("fewer_partitions", "nulls_exceed_share", "no_rows")
        if codes.count(code) > 1
    ]
    values = sorted(
        (p["value"], p["estimated_rows"]) for p in plan["predicates"] if p["kind"] == "value"
    )
    dominant = sorted((w["value"], w["rows"]) for w in warnings if w["code"] == "dominant_value")
    if dominant != values:
        problems.append("dominant_value warnings must name exactly the = predicates and their rows")
    fewer = [w for w in warnings if w["code"] == "fewer_partitions"]
    planned = plan["planned_partitions"]
    if fewer != (
        [{"code": "fewer_partitions", "requested": n, "planned": planned}] if planned < n else []
    ):
        problems.append(
            "fewer_partitions must be given, with requested and planned, exactly when planned < N"
        )
    nulls = [w for w in warnings if w["code"] == "nulls_exceed_share"]
    exceeds = inp["null_rows"] * n > rows["total"]
    if nulls != ([{"code": "nulls_exceed_share", "rows": inp["null_rows"]}] if exceeds else []):
        problems.append(
            "nulls_exceed_share must be given, with null_rows, exactly when null_rows * N > total"
        )
    if ("no_rows" in codes) != (rows["total"] == 0):
        problems.append("no_rows must be given exactly when total_rows is 0")
    return problems


def check_skew(n: int, plan: dict[str, Any]) -> list[str]:
    """Check the skew score against the largest predicate (spec section 7)."""
    total = plan["rows"]["total"]
    score = plan["skew_score"]
    if total == 0:
        return [] if score is None else ["skew_score must be null when total_rows is 0"]
    largest = max(p["estimated_rows"] for p in plan["predicates"])
    k = (2 * largest * n * 1000 + total) // (2 * total)
    if score is None or Decimal(score) * 1000 != k:
        return [
            f"skew_score must be {Decimal(k) / 1000}, from the largest predicate's {largest} rows"
        ]
    return []


def check_plan(inp: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """Check a plan against its own input."""
    problems = []
    for key, expected in (
        ("column", inp["column"]),
        ("type", inp["type"]),
        ("scale", inp.get("scale")),
        ("requested_partitions", inp["partitions"]),
        ("source", inp.get("source")),
    ):
        if plan.get(key) != expected:
            problems.append(f"{key} must be {expected!r}, from the input")
    histogram = inp["histogram"]
    non_null = sum(m["rows"] for m in inp["mcv"]) + (histogram["rows"] if histogram else 0)
    rows = {"total": inp["null_rows"] + non_null, "null": inp["null_rows"], "non_null": non_null}
    if plan["rows"] != rows:
        problems.append(f"rows must be {rows}, from the input")
    predicates = plan["predicates"]
    if predicates[0]["estimated_rows"] != inp["null_rows"]:
        problems.append("the NULL predicate must have null_rows estimated rows")
    if sum(p["estimated_rows"] for p in predicates) != rows["total"]:
        problems.append("estimated rows must add up to total_rows")
    planned = len(predicates) - 1
    if plan["planned_partitions"] != planned:
        problems.append(
            f"planned_partitions must be {planned}, the predicates besides the NULL one"
        )
    if planned > inp["partitions"]:
        problems.append("a plan never has more than N non-NULL predicates")
    problems += check_ranges(predicates)
    problems += check_warnings(inp, plan)
    problems += check_skew(inp["partitions"], plan)
    return problems


def check_statistics(source: dict[str, Any]) -> list[str]:
    """Check the warnings of a converted input's source (spec section 13)."""
    rows, modified = source["rows_estimate"], source["modified_since_analyze"]
    unestimated = source["unestimated_rows"]
    expected: list[dict[str, Any]] = []
    if modified is not None and modified * STALE_DIVISOR > rows:
        expected.append({"code": "stale_statistics", "modified_rows": modified, "rows": rows})
    if unestimated > 0:
        expected.append({"code": "special_values_left_out", "rows": unestimated})
    if source["warnings"] != expected:
        return [
            (
                f"source warnings must be {expected}, from rows_estimate, unestimated_rows"
                " and modified_since_analyze"
            )
        ]
    return []


def check_sample(source: dict[str, Any]) -> list[str]:
    """Check the warnings of an input converted from a sample (spec section 14)."""
    unestimated = source["unestimated_rows"]
    expected = [{"code": "special_values_left_out", "rows": unestimated}] if unestimated else []
    if source["warnings"] != expected:
        return [f"source warnings must be {expected}, from unestimated_rows"]
    return []


def check_hash(inp: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """Check a hash plan against its input (spec section 16): it follows from the counts."""
    n, nulls, non_null = inp["partitions"], inp["null_rows"], inp["non_null_rows"]
    total = nulls + non_null
    problems = [
        f"{key} must be {expected!r}, from the input"
        for key, expected in (
            ("column", inp["column"]),
            ("type", inp["type"]),
            ("requested_partitions", n),
            ("planned_partitions", n),
            ("rows", {"total": total, "null": nulls, "non_null": non_null}),
            ("source", inp.get("source")),
        )
        if plan.get(key) != expected
    ]
    quoted = '"' + inp["column"].replace('"', '""') + '"'
    remainder = f"((pg_catalog.hashtextextended({quoted}::text, 0) % {n}) + {n}) % {n}"
    expected = [{"kind": "null", "sql": f"{quoted} IS NULL", "estimated_rows": nulls}] + [
        {
            "kind": "hash",
            "remainder": i,
            "sql": f"{remainder} = {i}",
            "estimated_rows": (i + 1) * non_null // n - i * non_null // n,
        }
        for i in range(n)
    ]
    if plan["predicates"] != expected:
        problems.append("the predicates must be the NULL predicate and remainders 0 to N - 1")
    warnings: list[dict[str, Any]] = [{"code": "full_scans"}]
    if nulls * n > total:
        warnings.append({"code": "nulls_exceed_share", "rows": nulls})
    if total == 0:
        warnings.append({"code": "no_rows"})
    if plan["warnings"] != warnings:
        problems.append(f"warnings must be {warnings}, from the row counts")
    return problems + check_skew(n, plan)


def check_fixture(path: Path, version: str, *, kind: str = "plan") -> list[str]:
    """Return the problems with one fixture file, each prefixed with its name.

    ``kind`` is ``plan`` for the planner fixtures, or the name of the
    subdirectory in SUBDIRS that holds the fixture.
    """
    fixture = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    expected = fixture["expected"]
    is_error = "error" in expected
    problems = check_name(path.name, is_error=is_error)
    if expected["spec_version"] != version:
        problems.append(f"spec_version must be {version}, the version in {SPEC.as_posix()}")
    if kind == "plan" and not is_error:
        problems += check_plan(fixture["input"], expected)
    if kind == "pg-stats" and not is_error:
        problems += check_statistics(expected["input"]["source"])
    if kind == "sample" and not is_error:
        problems += check_sample(expected["input"]["source"])
    if kind == "hash" and not is_error:
        problems += check_hash(fixture["input"], expected)
    return [f"{path}: {problem}" for problem in problems]


def main(argv: list[str] | None = None) -> int:
    """Check every fixture and report the problems."""
    args = sys.argv[1:] if argv is None else argv
    directory = Path(args[0]) if args else DEFAULT_DIR
    version = spec_version(SPEC.read_text(encoding="utf-8"))
    files = [(path, "plan") for path in sorted(directory.iterdir()) if path.name not in SUBDIRS]
    for name in SUBDIRS:
        if (directory / name).is_dir():
            files += [(path, name) for path in sorted((directory / name).iterdir())]
    problems = []
    for path, kind in files:
        if path.suffix == ".json":
            problems += check_fixture(path, version, kind=kind)
        else:
            problems.append(f"{path}: only .json fixtures belong in {path.parent}")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"{len(files)} fixtures are consistent with spec version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
