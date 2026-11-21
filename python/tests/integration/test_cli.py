from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

import skewsplit
from skewsplit import cli, postgres
from skewsplit.core.planner import plan

if TYPE_CHECKING:
    from pathlib import Path

    from integration.conftest import Database

# 900 rows with values 1 to 900, and 100 NULL rows.
SETUP = """
CREATE TABLE {t} (v integer) WITH (autovacuum_enabled = false);
INSERT INTO {t} SELECT CASE WHEN g <= 900 THEN g END FROM generate_series(1, 1000) AS g;
ANALYZE {t}
"""
PLAN_4 = ("--column", "v", "--partitions", "4")
# A password to look for in the output; the server trusts every connection.
NEEDLE = "hunter2"


@pytest.fixture(scope="module")
def table(database: Database) -> str:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        for statement in SETUP.split(";\n"):
            admin.execute(sql.SQL(statement).format(t=sql.Identifier(database.schema, "cli_v")))
    return f"{database.schema}.cli_v"


def run(capsys: pytest.CaptureFixture[str], database: Database, *argv: str) -> tuple[int, str, str]:
    command, *rest = argv
    code = cli.main([command, "--dsn", database.dsn, *rest])
    out, err = capsys.readouterr()
    return code, out, err


def test_plan_prints_the_report(
    capsys: pytest.CaptureFixture[str], database: Database, table: str
) -> None:
    code, out, err = run(capsys, database, "plan", "--table", table, *PLAN_4)
    assert (code, err) == (cli.OK, "")
    assert out == postgres.plan(database.dsn, table, "v", 4).report + "\n"


def test_a_json_plan_verifies(
    capsys: pytest.CaptureFixture[str], database: Database, table: str, tmp_path: Path
) -> None:
    argv = ("plan", "--table", table, *PLAN_4, "--format", "json")
    code, out, _ = run(capsys, database, *argv)
    assert code == cli.OK
    path = tmp_path / "plan.json"
    path.write_text(out, encoding="utf-8")
    code, out, _ = run(capsys, database, "verify", "--table", table, "--plan", str(path))
    assert code == cli.OK
    assert out == f"OK: each of the 1000 rows of {table} matches exactly one predicate\n"
    counted = ("verify", "--table", table, "--plan", str(path), "--counts", "--format", "json")
    code, out, _ = run(capsys, database, *counted)
    report: Any = json.loads(out)
    assert (code, report["ok"]) == (cli.OK, True)
    assert sum(entry["actual_rows"] for entry in report["predicates"]) == 1000


def test_an_edited_plan_fails(
    capsys: pytest.CaptureFixture[str], database: Database, table: str, tmp_path: Path
) -> None:
    data = postgres.plan(database.dsn, table, "v", 4).to_dict()
    data["predicates"] = [
        {"sql": '"v" IS NULL', "estimated_rows": 100},
        {"sql": '"v" < 500', "estimated_rows": 450},
        {"sql": '"v" >= 400', "estimated_rows": 450},
    ]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    code, out, _ = run(capsys, database, "verify", "--table", table, "--plan", str(path))
    assert code == cli.VERIFY_FAILED
    assert out == (
        f"FAILED: of the 1000 rows of {table}, no predicate matches 0"
        " and more than one matches 100\n"
    )


def test_verify_plans_from_the_statistics(
    capsys: pytest.CaptureFixture[str], database: Database, table: str
) -> None:
    argv = ("verify", "--table", table, "--column", "v", "--partitions", "8", "--counts")
    code, out, _ = run(capsys, database, *argv)
    lines = out.split("\n")
    assert code == cli.OK
    assert lines[0] == f"OK: each of the 1000 rows of {table} matches exactly one predicate"
    assert lines[1] == "estimated  actual  predicate"
    assert lines[2].endswith('  100  "v" IS NULL')
    assert lines[-2].startswith("skew score: estimated ")


def test_stats_gives_what_plan_plans_from(
    capsys: pytest.CaptureFixture[str], database: Database, table: str
) -> None:
    expected = postgres.plan(database.dsn, table, "v", 4)
    code, out, _ = run(capsys, database, "stats", "--table", table, "--column", "v")
    assert code == cli.OK
    assert skewsplit.plan(json.loads(out), 4).parts == expected.parts
    argv = ("stats", "--table", table, *PLAN_4)
    code, out, _ = run(capsys, database, *argv)
    assert code == cli.OK
    assert plan(json.loads(out)).parts == expected.parts


def test_a_missing_table(capsys: pytest.CaptureFixture[str], database: Database) -> None:
    argv = ("plan", "--table", f"{database.schema}.no_such_table", *PLAN_4)
    code, out, err = run(capsys, database, *argv)
    assert (code, out) == (cli.INVALID, "")
    assert err.startswith("skewsplit: table_not_found: ")


def test_a_query_that_times_out(
    capsys: pytest.CaptureFixture[str], database: Database, table: str, tmp_path: Path
) -> None:
    data = postgres.plan(database.dsn, table, "v", 1).to_dict()
    data["predicates"] = [
        {"sql": '"v" IS NULL', "estimated_rows": 100},
        {"sql": "(SELECT true FROM pg_sleep(2))", "estimated_rows": 900},
    ]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    argv = ("verify", "--table", table, "--plan", str(path), "--statement-timeout-ms", "100")
    code, out, err = run(capsys, database, *argv)
    assert (code, out) == (cli.DATABASE, "")
    # The message is PostgreSQL's, which may add lines of context.
    assert err.startswith("skewsplit: canceling statement due to statement timeout")


def test_a_failed_connection_does_not_show_the_password(
    capsys: pytest.CaptureFixture[str], database: Database
) -> None:
    # Nothing listens on port 1, so the connection is refused.
    dsn = make_conninfo(database.dsn, port="1", password=NEEDLE, connect_timeout="5")
    code = cli.main(["plan", "--dsn", dsn, "--table", "t", *PLAN_4])
    out, err = capsys.readouterr()
    assert (code, out) == (cli.DATABASE, "")
    assert err.startswith("skewsplit: cannot connect: ")
    assert NEEDLE not in err
