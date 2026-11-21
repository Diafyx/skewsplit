"""The command line, against fakes of the PostgreSQL adapter.

tests/integration runs the same commands against a real database.
"""

from __future__ import annotations

import builtins
import io
import json
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

from skewsplit import cli, postgres
from skewsplit.core.errors import SpecError
from skewsplit.core.planner import plan
from skewsplit.postgres.catalog import CatalogError
from skewsplit.postgres.verify import Verification, verification_report

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

SECTION_1 = {
    "column": "customer_id",
    "type": "int4",
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
    "source": {"statistics": "pg_stats", "table": "sales.orders"},
}
PLAN = plan({**SECTION_1, "partitions": 4})
TABLE = "sales.orders"
COUNTS = (100, 80, 500, 190, 190)


@dataclass
class Fake:
    """Stands in for the database: records the calls and answers them."""

    calls: list[tuple[object, ...]] = field(default_factory=list)
    found: Verification = field(default_factory=lambda: Verification(TABLE, 1060, 0, 0))
    error: Exception | None = None

    def answer(self, *call: object) -> None:
        self.calls.append(call)
        if self.error is not None:
            raise self.error


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    db = Fake()
    conn = object()

    def connect(dsn: str, secrets: set[str]) -> nullcontext[object]:
        db.calls.append(("connect", dsn, secrets))
        return nullcontext(conn)

    def fake_plan(used: object, table: str, column: str, partitions: int) -> object:
        assert used is conn
        db.answer("plan", table, column, partitions)
        return replace(PLAN, requested_partitions=partitions)

    def fake_distribution(used: object, table: str, column: str) -> dict[str, object]:
        assert used is conn
        db.answer("distribution", table, column)
        return dict(SECTION_1)

    def fake_verify(
        used: object, table: str, column: str, predicates: list[str], **options: object
    ) -> Verification:
        assert used is conn
        db.answer("verify", table, column, predicates, options)
        if options["counts"] is True:
            return replace(db.found, predicate_rows=COUNTS)
        return db.found

    monkeypatch.setattr(cli, "_connect", connect)
    monkeypatch.setattr(postgres, "plan", fake_plan)
    monkeypatch.setattr(postgres, "distribution", fake_distribution)
    monkeypatch.setattr(postgres, "verify", fake_verify)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    return db


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def usage_error(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit) as caught:
        cli.main(list(argv))
    assert caught.value.code == cli.USAGE
    return capsys.readouterr().err


PLAN_ARGS = ("plan", "--table", TABLE, "--column", "customer_id", "--partitions", "4")


# Help and arguments


@pytest.mark.parametrize("argv", [["--help"], ["plan", "--help"], ["verify", "--help"]])
def test_the_help_warns_about_real_values_and_lists_the_exit_codes(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "The output contains real values from the column" in out
    assert "PGPASSWORD" in out
    for code in range(5):
        assert f"\n  {code}  " in out


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    assert "required: COMMAND" in usage_error(capsys)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--plan", "p.json", "--column", "c"], "not both"),
        (["--plan", "p.json", "--partitions", "4"], "not both"),
        ([], "verify needs --plan, or --column and --partitions"),
        (["--column", "c"], "verify needs --plan"),
        (["--partitions", "4"], "verify needs --plan"),
    ],
)
def test_verify_takes_a_plan_or_a_column_to_plan(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert message in usage_error(capsys, "verify", "--table", TABLE, *argv)


@pytest.mark.parametrize("timeout", ["0", "-5", "abc"])
def test_the_statement_timeout_is_positive(
    capsys: pytest.CaptureFixture[str], timeout: str
) -> None:
    argv = ["verify", "--table", TABLE, "--plan", "-", "--statement-timeout-ms", timeout]
    err = usage_error(capsys, *argv)
    assert f"must be a positive whole number, not {timeout!r}" in err


# Connecting


@pytest.mark.parametrize(
    "dsn", ["postgresql://u:s%zz@h/db", "host=h password s%zz", "host=h password='s%zz"]
)
def test_an_invalid_dsn_is_not_quoted(capsys: pytest.CaptureFixture[str], dsn: str) -> None:
    code, out, err = run(capsys, *PLAN_ARGS, "--dsn", dsn)
    assert code == cli.USAGE
    assert out == ""
    assert err == "skewsplit: --dsn is not a valid libpq connection string or URI\n"


def test_a_connection_error_hides_the_passwords(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[str, dict[str, object]]] = []

    def connect(dsn: str, **options: object) -> None:
        opened.append((dsn, options))
        msg = 'connection failed: password "hunter2" or "from-env" for "u" was refused'
        raise psycopg.OperationalError(msg)

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setenv("PGPASSWORD", "from-env")
    dsn = "postgresql://u:hunter2@db.invalid/shop"
    code, out, err = run(capsys, *PLAN_ARGS, "--dsn", dsn)
    assert code == cli.DATABASE
    assert out == ""
    assert err == (
        'skewsplit: cannot connect: connection failed: password "********" or "********"'
        ' for "u" was refused\n'
    )
    assert opened == [(dsn, {"autocommit": True})]


def test_without_a_dsn_the_environment_is_used(
    capsys: pytest.CaptureFixture[str], fake: Fake
) -> None:
    assert run(capsys, *PLAN_ARGS)[0] == cli.OK
    assert fake.calls[0] == ("connect", "", set())


def test_the_passwords_to_hide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "env")
    assert cli.passwords("password=dsn") == {"dsn", "env"}
    monkeypatch.setenv("PGPASSWORD", "")
    assert cli.passwords("host=h") == set()
    # A longer secret is replaced first, so a shorter one inside it cannot leave a part.
    assert cli.redact("abc and ab", {"ab", "abc"}) == "******** and ********"


def reimport_without(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    real_import = builtins.__import__

    def failing_import(name: str, *args: Any, **kwargs: Any) -> ModuleType:  # noqa: ANN401 -- as builtins.__import__
        if name == "psycopg":
            msg = f"No module named {missing!r}"
            raise ModuleNotFoundError(msg, name=missing)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)


def test_the_commands_need_the_postgres_extra(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    reimport_without(monkeypatch, "psycopg")
    code, _, err = run(capsys, *PLAN_ARGS)
    assert code == cli.USAGE
    assert err == "skewsplit: this command needs psycopg: pip install 'skewsplit[postgres]'\n"


def test_a_module_missing_inside_the_driver_keeps_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reimport_without(monkeypatch, "typing_extensions")
    with pytest.raises(ModuleNotFoundError, match="typing_extensions"):
        cli.main(list(PLAN_ARGS))


# plan and stats


def test_plan_prints_the_report(capsys: pytest.CaptureFixture[str], fake: Fake) -> None:
    code, out, err = run(capsys, *PLAN_ARGS, "--dsn", "dbname=shop")
    assert (code, out, err) == (cli.OK, PLAN.report + "\n", "")
    assert fake.calls == [
        ("connect", "dbname=shop", set()),
        ("plan", TABLE, "customer_id", 4),
    ]


def test_plan_prints_json(capsys: pytest.CaptureFixture[str], fake: Fake) -> None:
    code, out, _ = run(capsys, *PLAN_ARGS, "--format", "json")
    assert code == cli.OK
    assert out == PLAN.to_json(indent=2) + "\n"
    assert fake.calls[-1] == ("plan", TABLE, "customer_id", 4)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (CatalogError("statistics_missing", "run ANALYZE"), "statistics_missing: run ANALYZE"),
        (SpecError("invalid_partitions", "partitions is 0"), "invalid_partitions: partitions is 0"),
    ],
)
def test_a_column_that_cannot_be_planned(
    capsys: pytest.CaptureFixture[str], fake: Fake, error: Exception, message: str
) -> None:
    fake.error = error
    assert run(capsys, *PLAN_ARGS) == (cli.INVALID, "", f"skewsplit: {message}\n")


def test_a_query_that_fails(
    capsys: pytest.CaptureFixture[str], fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGPASSWORD", "hunter2")
    fake.error = psycopg.errors.QueryCanceled("canceling statement hunter2\n")
    assert run(capsys, *PLAN_ARGS) == (
        cli.DATABASE,
        "",
        "skewsplit: canceling statement ********\n",
    )


def test_stats_prints_the_distribution(capsys: pytest.CaptureFixture[str], fake: Fake) -> None:
    code, out, _ = run(capsys, "stats", "--table", TABLE, "--column", "customer_id")
    assert code == cli.OK
    assert json.loads(out) == SECTION_1
    assert fake.calls[-1] == ("distribution", TABLE, "customer_id")


def test_stats_with_partitions_prints_a_planner_input(
    capsys: pytest.CaptureFixture[str], fake: Fake
) -> None:
    argv = ("stats", "--table", TABLE, "--column", "customer_id", "--partitions", "4")
    code, out, _ = run(capsys, *argv)
    assert code == cli.OK
    data = json.loads(out)
    assert list(data)[:3] == ["column", "type", "partitions"]
    assert plan(data) == PLAN
    assert fake.calls[-1] == ("distribution", TABLE, "customer_id")


def test_stats_checks_the_partitions(capsys: pytest.CaptureFixture[str], fake: Fake) -> None:
    argv = ("stats", "--table", TABLE, "--column", "customer_id", "--partitions", "0")
    code, out, err = run(capsys, *argv)
    assert (code, out) == (cli.INVALID, "")
    assert err.startswith("skewsplit: invalid_partitions: ")
    assert fake.calls[-1] == ("distribution", TABLE, "customer_id")


# verify

VERIFY_ARGS = ("verify", "--table", TABLE)
FRESH = ("--column", "customer_id", "--partitions", "4")


def test_verify_plans_then_checks(capsys: pytest.CaptureFixture[str], fake: Fake) -> None:
    code, out, err = run(capsys, *VERIFY_ARGS, *FRESH, "--statement-timeout-ms", "500")
    assert (code, err) == (cli.OK, "")
    assert out == f"OK: each of the 1060 rows of {TABLE} matches exactly one predicate\n"
    assert fake.calls[1:] == [
        ("plan", TABLE, "customer_id", 4),
        (
            "verify",
            TABLE,
            "customer_id",
            PLAN.predicates,
            {"counts": False, "statement_timeout_ms": 500},
        ),
    ]


def test_verify_reports_rows_that_do_not_match_once(
    capsys: pytest.CaptureFixture[str], fake: Fake
) -> None:
    fake.found = Verification(TABLE, 1060, 3, 7)
    code, out, _ = run(capsys, *VERIFY_ARGS, *FRESH)
    assert code == cli.VERIFY_FAILED
    assert out == (
        f"FAILED: of the 1060 rows of {TABLE}, no predicate matches 3 and more than one matches 7\n"
    )


def test_verify_with_counts(capsys: pytest.CaptureFixture[str], fake: Fake) -> None:
    code, out, _ = run(capsys, *VERIFY_ARGS, *FRESH, "--counts")
    assert code == cli.OK
    assert out.split("\n")[1:] == [
        "estimated  actual  predicate",
        '      100     100  "customer_id" IS NULL',
        '       76      80  "customer_id" < 42',
        '      500     500  "customer_id" = 42',
        '      192     190  "customer_id" > 42 AND "customer_id" < 520',
        '      192     190  "customer_id" >= 520',
        "skew score: estimated 1.887, actual 1.887",
        "",
    ]
    assert fake.calls[-1][-1] == {"counts": True, "statement_timeout_ms": None}


def test_verify_with_counts_of_an_empty_table(
    capsys: pytest.CaptureFixture[str], fake: Fake
) -> None:
    fake.found = Verification(TABLE, 0, 0, 0)
    code, out, _ = run(capsys, *VERIFY_ARGS, *FRESH, "--counts")
    assert code == cli.OK
    assert out.split("\n")[-2] == "skew score: estimated 1.887, actual none"


def test_verify_prints_json(capsys: pytest.CaptureFixture[str], fake: Fake) -> None:
    fake.found = Verification(TABLE, 1060, 1, 0)
    code, out, _ = run(capsys, *VERIFY_ARGS, *FRESH, "--counts", "--format", "json")
    assert code == cli.VERIFY_FAILED
    estimates = [(part.sql, part.estimated_rows) for part in PLAN.parts]
    expected = verification_report(
        replace(fake.found, predicate_rows=COUNTS), estimates, 4, PLAN.skew_score
    )
    assert json.loads(out) == expected


def write_plan(tmp_path: Path, data: object) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_verify_a_plan_file(capsys: pytest.CaptureFixture[str], fake: Fake, tmp_path: Path) -> None:
    edited = PLAN.to_dict()
    edited["predicates"][1]["sql"] = '"customer_id" <= 42'  # type: ignore[index]
    code, out, _ = run(capsys, *VERIFY_ARGS, "--plan", write_plan(tmp_path, edited), "--counts")
    assert code == cli.OK
    assert '       76      80  "customer_id" <= 42' in out.split("\n")
    # The plan file says which predicates to check; nothing is planned.
    assert [call[0] for call in fake.calls] == ["connect", "verify"]
    _, _, column, predicates, _ = fake.calls[-1]
    assert column == "customer_id"
    assert isinstance(predicates, list)
    assert predicates[1] == '"customer_id" <= 42'


def test_verify_a_plan_from_standard_input(
    capsys: pytest.CaptureFixture[str], fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(PLAN.to_json()))
    code, _, _ = run(capsys, *VERIFY_ARGS, "--plan", "-")
    assert code == cli.OK
    assert fake.calls[-1][3] == PLAN.predicates


@pytest.mark.parametrize("skew", [None, 2, 1.5])
def test_a_plan_file_skew_score_is_a_number_or_null(
    capsys: pytest.CaptureFixture[str], fake: Fake, tmp_path: Path, skew: object
) -> None:
    data = {**PLAN.to_dict(), "skew_score": skew}
    argv = (*VERIFY_ARGS, "--plan", write_plan(tmp_path, data), "--counts", "--format", "json")
    code, out, _ = run(capsys, *argv)
    assert code == cli.OK
    assert json.loads(out)["skew_score"]["estimated"] == skew
    assert len(fake.calls) == 2


def plan_with(**changes: object) -> dict[str, object]:
    return {**PLAN.to_dict(), **changes}


def predicates_with(**changes: object) -> list[object]:
    return [{**PLAN.parts[0].to_dict(), **changes}]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "is not a JSON object"),
        (plan_with(spec_version="2.0"), "needs spec_version '1.0'"),
        ({"column": "c"}, "needs spec_version '1.0'"),
        (plan_with(column=None), "needs a column and requested_partitions"),
        (plan_with(requested_partitions="4"), "needs a column and requested_partitions"),
        (plan_with(requested_partitions=True), "needs a column and requested_partitions"),
        (plan_with(predicates=[]), "needs a list of predicates"),
        (plan_with(predicates={"sql": "true"}), "needs a list of predicates"),
        (plan_with(predicates=["true"]), "needs the sql and estimated_rows"),
        (plan_with(predicates=predicates_with(sql=1)), "needs the sql and estimated_rows"),
        (plan_with(predicates=predicates_with(estimated_rows=False)), "needs the sql"),
        (plan_with(skew_score="1.887"), "needs a skew_score that is a number or null"),
        (plan_with(skew_score=True), "needs a skew_score that is a number or null"),
    ],
)
def test_an_invalid_plan_file(
    capsys: pytest.CaptureFixture[str], fake: Fake, tmp_path: Path, data: object, message: str
) -> None:
    path = write_plan(tmp_path, data)
    code, out, err = run(capsys, *VERIFY_ARGS, "--plan", path)
    assert (code, out) == (cli.INVALID, "")
    assert err.startswith(f"skewsplit: the plan {path} {message}")
    # Nothing connects for a plan that cannot be read.
    assert fake.calls == []


def test_a_plan_file_that_cannot_be_read(
    capsys: pytest.CaptureFixture[str], fake: Fake, tmp_path: Path
) -> None:
    missing = str(tmp_path / "missing.json")
    assert run(capsys, *VERIFY_ARGS, "--plan", missing) == (
        cli.INVALID,
        "",
        f"skewsplit: cannot read the plan {missing}: No such file or directory\n",
    )
    assert fake.calls == []


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe"])
def test_a_plan_file_that_is_not_json(
    capsys: pytest.CaptureFixture[str], fake: Fake, tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "plan.json"
    path.write_bytes(content)
    code, _, err = run(capsys, *VERIFY_ARGS, "--plan", str(path))
    assert code == cli.INVALID
    assert err.startswith(f"skewsplit: the plan {path} is not JSON: ")
    assert fake.calls == []


def test_a_plan_on_standard_input_that_is_not_json(
    capsys: pytest.CaptureFixture[str], fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("[1,"))
    code, _, err = run(capsys, *VERIFY_ARGS, "--plan", "-")
    assert code == cli.INVALID
    assert err.startswith("skewsplit: the plan on standard input is not JSON: ")
    assert fake.calls == []
