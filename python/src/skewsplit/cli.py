"""The ``skewsplit`` command line.

``plan`` prints a column's predicates and skew report, ``verify`` checks that
every row of the table matches exactly one predicate, and ``stats`` prints the
column's distribution as JSON, for other tools to plan from.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from skewsplit import __version__
from skewsplit.core.errors import SpecError
from skewsplit.core.inputs import parse_input
from skewsplit.core.plan import SPEC_VERSION, Plan

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import psycopg

    from skewsplit.postgres.verify import Verification

    # What verify needs of a plan: the column, each predicate's SQL and
    # estimated rows, the requested partitions and the estimated skew score.
    PlanFile = tuple[str, list[tuple[str, int]], int, float | None]

# skewsplit.postgres.STRATEGIES and the default sample size, without importing
# psycopg, which the postgres extra installs.
STRATEGIES = ("statistics", "sample", "exact", "auto")
METHODS = ("range", "hash")
SAMPLE_ROWS = 30000

OK = 0
VERIFY_FAILED = 1
USAGE = 2
INVALID = 3
DATABASE = 4

EPILOG = f"""\
The output contains real values from the column: treat it like the data.

Connect with --dsn, a libpq connection string or URI, or leave it out to use
the PG* environment variables. A password is better kept in PGPASSWORD or
~/.pgpass than on the command line, where other users can see it.

exit codes:
  {OK}  success
  {VERIFY_FAILED}  verify found rows that match no predicate, or more than one
  {USAGE}  the command line is wrong, or skewsplit[postgres] is not installed
  {INVALID}  the column cannot be planned, or the plan file is not valid
  {DATABASE}  the database could not be reached, or a query failed or timed out
"""


_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_MAJOR, _MINOR = SPEC_VERSION.split(".")

_PLAN_FORMATS: dict[str, Callable[[Plan], str]] = {
    "text": lambda plan: plan.report,
    "json": lambda plan: plan.to_json(indent=2),
    "pyspark": Plan.to_pyspark,
    "scala": Plan.to_scala,
}


class CommandError(Exception):
    """A command failed: print ``message`` and exit with ``code``."""

    def __init__(self, code: int, message: str) -> None:
        """Create a failure with an exit code."""
        super().__init__(message)
        self.code = code
        self.message = message


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the ``skewsplit`` command."""
    parser = argparse.ArgumentParser(
        prog="skewsplit",
        description="Plan balanced JDBC partition predicates for Spark.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"skewsplit {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def command(name: str, summary: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(
            name,
            help=summary,
            description=f"{summary}.",
            epilog=EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sub.add_argument("--dsn", default="", help="libpq connection string or URI")
        sub.add_argument("--table", required=True, help="the table, as SQL names it")
        sub.add_argument(
            "--where",
            metavar="SQL",
            help="plan or check a filtered read: only the rows this condition keeps."
            " It is SQL run as it is, in a read-only transaction: pass only text you trust",
        )
        return sub

    plan = command("plan", "Plan predicates from the column's statistics, a sample or every row")
    plan.add_argument("--column", required=True, help="the column to split, not quoted")
    plan.add_argument("--partitions", required=True, type=int, metavar="N")
    _method_option(plan)
    _planning_options(plan)
    plan.add_argument(
        "--format",
        choices=tuple(_PLAN_FORMATS),
        default="text",
        help="the skew report, the plan as JSON, or PySpark or Scala code that reads the table",
    )

    verify = command("verify", "Check that every row of the table matches exactly one predicate")
    verify.add_argument(
        "--plan",
        metavar="FILE",
        help="a plan from `plan --format json`, or - for standard input;"
        " without it, plan from the statistics with --column and --partitions",
    )
    verify.add_argument("--column", help="the column to split, not quoted")
    verify.add_argument("--partitions", type=int, metavar="N")
    _method_option(verify)
    _planning_options(verify, timeout=False)
    verify.add_argument(
        "--counts",
        action="store_true",
        help="also count each predicate's rows, one query each, for the actual skew",
    )
    verify.add_argument(
        "--statement-timeout-ms",
        type=_positive,
        metavar="MS",
        help="cancel a query, of the check or of planning, that runs longer than this",
    )
    verify.add_argument("--format", choices=("text", "json"), default="text")

    stats = command(
        "stats", "Print the column's distribution as JSON (spec section 1), as plan reads it"
    )
    stats.add_argument("--column", required=True, help="the column, not quoted")
    stats.add_argument(
        "--partitions",
        type=int,
        metavar="N",
        help="add partitions, which makes the output a complete planner input",
    )
    _planning_options(stats)
    return parser


def _method_option(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--method",
        choices=METHODS,
        help="range: ranges of the column's values, which an index can serve (the default);"
        " hash: one remainder of a hash of each value's text per partition, for a column"
        " of any type, but every partition reads the whole table",
    )


def _planning_options(sub: argparse.ArgumentParser, *, timeout: bool = True) -> None:
    sub.add_argument(
        "--strategy",
        choices=STRATEGIES,
        help="statistics: read pg_stats, never the table (the default);"
        " sample: read a sample of the table with TABLESAMPLE,"
        " for when the statistics are missing or out of date;"
        " exact: read every row, which scans the whole table three times and sorts the column;"
        " auto: the statistics when they are fresh, otherwise a sample, and hash predicates"
        " for a column that ranges cannot split, and say why",
    )
    sub.add_argument(
        "--sample-rows",
        type=_positive,
        metavar="N",
        help="for --strategy sample or auto: about how many rows to sample"
        f" (default {SAMPLE_ROWS})",
    )
    sub.add_argument(
        "--max-sample-rows",
        type=_positive,
        metavar="N",
        help="for --strategy sample or auto: the most rows the sample reads"
        " (default 10 times --sample-rows)",
    )
    sub.add_argument(
        "--seed",
        type=int,
        help="for --strategy sample or auto: the same seed gives the same sample of the same table",
    )
    if timeout:
        sub.add_argument(
            "--statement-timeout-ms",
            type=_positive,
            metavar="MS",
            help="cancel a query that runs longer than this",
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    sampling = (args.sample_rows, args.max_sample_rows, args.seed) != (None, None, None)
    method = getattr(args, "method", None)
    if args.command == "verify":
        if args.plan is not None and (args.column is not None or args.partitions is not None):
            parser.error("verify takes --plan, or --column and --partitions, not both")
        if args.plan is None and (args.column is None or args.partitions is None):
            parser.error("verify needs --plan, or --column and --partitions")
        if args.plan is not None and (method is not None or args.strategy is not None or sampling):
            parser.error("--method, --strategy and its options are for planning, not for --plan")
    if method == "hash" and (args.strategy is not None or sampling):
        parser.error("--strategy and its options are for --method range")
    if sampling and args.strategy not in {"sample", "auto"}:
        parser.error("--sample-rows, --max-sample-rows and --seed need --strategy sample or auto")
    smallest = SAMPLE_ROWS if args.sample_rows is None else args.sample_rows
    if args.max_sample_rows is not None and args.max_sample_rows < smallest:
        parser.error(f"--max-sample-rows must be at least the sample size, {smallest}")
    try:
        output, code = _run(args)
    except CommandError as error:
        sys.stderr.write(f"skewsplit: {error.message}\n")
        return error.code
    sys.stdout.write(output + "\n")
    return code


def _run(args: argparse.Namespace) -> tuple[str, int]:
    plan_file: PlanFile | None = None
    if args.command == "verify" and args.plan is not None:
        plan_file = _read_plan(args.plan)
    try:
        import psycopg  # noqa: PLC0415 -- the postgres extra is optional

        from skewsplit import postgres  # noqa: PLC0415
        from skewsplit.postgres.verify import verification_report  # noqa: PLC0415
    except ModuleNotFoundError as error:
        if error.name != "psycopg":
            raise
        message = "this command needs psycopg: pip install 'skewsplit[postgres]'"
        raise CommandError(USAGE, message) from None
    secrets = passwords(args.dsn)
    options = _options(args)
    try:
        with _connect(args.dsn, secrets) as conn:
            if args.command == "plan":
                result = postgres.plan(conn, args.table, args.column, args.partitions, **options)
                return _PLAN_FORMATS[args.format](result), OK
            if args.command == "stats":
                data = postgres.distribution(conn, args.table, args.column, **options)
                return _stats(data, args), OK
            if plan_file is None:
                planned = postgres.plan(conn, args.table, args.column, args.partitions, **options)
                plan_file = (
                    planned.column,
                    [(part.sql, part.estimated_rows) for part in planned.parts],
                    planned.requested_partitions,
                    planned.skew_score,
                )
            column, estimates, partitions, skew = plan_file
            found = postgres.verify(
                conn,
                args.table,
                column,
                [predicate for predicate, _ in estimates],
                counts=args.counts,
                statement_timeout_ms=args.statement_timeout_ms,
                where=args.where,
            )
    except (SpecError, postgres.CatalogError) as error:
        raise CommandError(INVALID, f"{error.code}: {error.message}") from None
    except psycopg.Error as error:
        raise CommandError(DATABASE, redact(str(error).strip(), secrets)) from None
    code = OK if found.ok else VERIFY_FAILED
    if args.format == "json":
        report = verification_report(found, estimates, partitions, skew)
        return json.dumps(report, indent=2, ensure_ascii=False), code
    return _verify_text(found, estimates, partitions, skew), code


def _options(args: argparse.Namespace) -> dict[str, Any]:
    """The keyword arguments of postgres.plan and postgres.distribution that were given."""
    options: dict[str, Any] = {}
    method = getattr(args, "method", None)
    if method is not None:
        options["method"] = method
    # The statistics are the default, so they need no option.
    if args.strategy in {"sample", "exact", "auto"}:
        options["strategy"] = args.strategy
    if args.strategy in {"sample", "auto"}:
        given = {"sample_rows": args.sample_rows, "max_sample_rows": args.max_sample_rows}
        options.update({key: value for key, value in given.items() if value is not None})
        if args.seed is not None:
            options["seed"] = args.seed
    if args.statement_timeout_ms is not None:
        options["statement_timeout_ms"] = args.statement_timeout_ms
    if args.where is not None:
        options["where"] = args.where
    return options


def _read_plan(name: str) -> PlanFile:
    """Read the parts of a JSON plan that verify needs."""
    where = "the plan on standard input" if name == "-" else f"the plan {name}"
    data = _load(name, where)
    if not isinstance(data, dict):
        _invalid(f"{where} is not a JSON object")
    if not readable(data.get("spec_version")):
        _invalid(
            f"{where} needs a spec_version from {_MAJOR}.0 to {SPEC_VERSION},"
            " as `skewsplit plan` writes"
        )
    column = data.get("column")
    partitions = _integer(data.get("requested_partitions"))
    predicates = data.get("predicates")
    if not isinstance(column, str) or partitions is None:
        _invalid(f"{where} needs a column and requested_partitions")
    if not isinstance(predicates, list) or not predicates:
        _invalid(f"{where} needs a list of predicates")
    estimates = []
    for predicate in predicates:
        fields = predicate if isinstance(predicate, dict) else {}
        sql, rows = fields.get("sql"), _integer(fields.get("estimated_rows"))
        if not isinstance(sql, str) or rows is None:
            _invalid(f"{where} needs the sql and estimated_rows of every predicate")
        estimates.append((sql, rows))
    skew = data.get("skew_score")
    if skew is None:
        return column, estimates, partitions, None
    if isinstance(skew, bool) or not isinstance(skew, (int, float)):
        _invalid(f"{where} needs a skew_score that is a number or null")
    return column, estimates, partitions, float(skew)


def readable(version: object) -> bool:
    """Whether verify reads a plan of this spec version.

    That is every version with the same major version and a minor version up
    to this one's, which only add keys (spec section 11).
    """
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    return match is not None and match[1] == _MAJOR and int(match[2]) <= int(_MINOR)


def _load(name: str, where: str) -> object:
    try:
        text = sys.stdin.read() if name == "-" else Path(name).read_text(encoding="utf-8")
        return json.loads(text)
    except OSError as error:
        _invalid(f"cannot read {where}: {error.strerror}")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _invalid(f"{where} is not JSON: {error}")


def _stats(data: dict[str, object], args: argparse.Namespace) -> str:
    if args.partitions is not None:
        # In the order of spec section 1, and checked as a planner input.
        data = {
            **{key: data[key] for key in ("column", "type", "scale") if key in data},
            "partitions": args.partitions,
            **data,
        }
        parse_input(data)
    return json.dumps(data, indent=2, ensure_ascii=False)


def _verify_text(
    found: Verification,
    estimates: list[tuple[str, int]],
    partitions: int,
    skew: float | None,
) -> str:
    summary = (
        f"OK: each of the {found.total_rows} rows of {found.table} matches exactly one predicate"
    )
    if not found.ok:
        summary = (
            f"FAILED: of the {found.total_rows} rows of {found.table}, no predicate matches"
            f" {found.unmatched_rows} and more than one matches {found.overlapping_rows}"
        )
    lines = [summary]
    if found.predicate_rows is not None:
        counted = zip(estimates, found.predicate_rows)
        rows = [(str(estimated), str(actual)) for (_, estimated), actual in counted]
        left = max(len("estimated"), *(len(estimated) for estimated, _ in rows))
        right = max(len("actual"), *(len(actual) for _, actual in rows))
        lines.append(f"{'estimated':>{left}}  {'actual':>{right}}  predicate")
        lines.extend(
            f"{estimated:>{left}}  {actual:>{right}}  {sql}"
            for (estimated, actual), (sql, _) in zip(rows, estimates)
        )
        actual_skew = found.skew_milli(partitions)
        lines.append(
            f"skew score: estimated {_score(skew)},"
            f" actual {_score(None if actual_skew is None else actual_skew / 1000)}"
        )
    return "\n".join(lines)


def _score(skew: float | None) -> str:
    return "none" if skew is None else f"{skew:.3f}"


def passwords(dsn: str) -> set[str]:
    """Return the passwords that error messages must not show."""
    import psycopg  # noqa: PLC0415
    from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415

    try:
        params = conninfo_to_dict(dsn)
    except psycopg.Error:
        # Not libpq's message: it can quote any part of the string, the password too.
        raise CommandError(USAGE, "--dsn is not a valid libpq connection string or URI") from None
    found = {str(params.get("password") or ""), os.environ.get("PGPASSWORD", "")}
    return found - {""}


def _connect(dsn: str, secrets: set[str]) -> psycopg.Connection[Any]:
    import psycopg  # noqa: PLC0415

    try:
        return psycopg.connect(dsn, autocommit=True)
    except psycopg.Error as error:
        message = redact(str(error).strip(), secrets)
        raise CommandError(DATABASE, f"cannot connect: {message}") from None


def redact(message: str, secrets: set[str]) -> str:
    """Return ``message`` with each of ``secrets`` replaced, the longest first."""
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, "********")
    return message


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        msg = f"must be a positive whole number, not {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _invalid(message: str) -> NoReturn:
    raise CommandError(INVALID, message)
