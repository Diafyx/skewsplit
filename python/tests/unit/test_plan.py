from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from skewsplit.core.planner import plan

if TYPE_CHECKING:
    from skewsplit.core.plan import Plan

SECTION_1 = {
    "column": "customer_id",
    "type": "int4",
    "partitions": 4,
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
}
SUMMARY = (
    "customer_id (int4): 1060 estimated rows, 4 of 4 partitions plus one for NULL, skew score 1.887"
)
DOMINANT = (
    "warning dominant_value: 42 holds 500 rows, at least one partition's share;"
    " it cannot be split further on this column"
)


def test_predicates_are_the_sql_of_each_part_as_a_list() -> None:
    result = plan(SECTION_1)
    # A list, as PySpark's DataFrameReader.jdbc declares its predicates.
    assert isinstance(result.predicates, list)
    assert result.predicates == [part.sql for part in result.parts]
    assert result.predicates[2] == '"customer_id" = 42'


def test_the_report_of_section_1() -> None:
    report = plan(SECTION_1).report
    assert report.split("\n") == [
        SUMMARY,
        "rows  predicate",
        ' 100  "customer_id" IS NULL',
        '  76  "customer_id" < 42',
        ' 500  "customer_id" = 42',
        ' 192  "customer_id" > 42 AND "customer_id" < 520',
        ' 192  "customer_id" >= 520',
        DOMINANT,
    ]


def test_the_report_aligns_wide_row_counts_and_shows_the_scale() -> None:
    result = plan(
        {
            "column": "amount",
            "type": "numeric",
            "scale": 2,
            "partitions": 2,
            "null_rows": 7,
            "mcv": [],
            "histogram": {"bounds": ["1.00", "3.00"], "rows": 20000},
        }
    )
    summary = (
        "amount (numeric, scale 2): 20007 estimated rows, 2 of 2 partitions plus one for NULL,"
        " skew score 1.000"
    )
    assert result.report.split("\n") == [
        summary,
        " rows  predicate",
        '    7  "amount" IS NULL',
        '10000  "amount" < 2.00',
        '10000  "amount" >= 2.00',
    ]


def test_the_report_of_a_plan_without_rows() -> None:
    empty = {"column": "c", "type": "int4", "partitions": 3, "null_rows": 0, "mcv": []}
    lines = plan({**empty, "histogram": None}).report.split("\n")
    assert lines[0] == (
        "c (int4): 0 estimated rows, 1 of 3 partitions plus one for NULL, no skew score"
    )
    assert lines[1:4] == ["rows  predicate", '   0  "c" IS NULL', '   0  "c" IS NOT NULL']
    assert [line.split(":")[0] for line in lines[4:]] == [
        "warning fewer_partitions",
        "warning no_rows",
    ]


def test_the_report_adds_the_well_formed_source_warnings() -> None:
    result = replace(
        plan(SECTION_1),
        source={
            "statistics": "pg_stats",
            "warnings": [
                {"code": "stale_statistics", "modified_rows": 200, "rows": 1060, "message": "old"},
                "not a warning",
                {"code": 7, "message": "a code that is not text"},
                {"code": "no_message"},
            ],
        },
    )
    assert result.report.split("\n")[-2:] == [DOMINANT, "warning stale_statistics: old"]


@pytest.mark.parametrize(
    "source", [None, "pg_stats", {"statistics": "pg_stats"}, {"warnings": "none"}]
)
def test_a_source_without_a_list_of_warnings_adds_none(source: object) -> None:
    result = replace(plan(SECTION_1), source=source)
    assert result.report == plan(SECTION_1).report


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ({"sample_rows": 30012}, "from a sample of 30012 rows"),
        ({"sample_rows": 0, "planning_ms": 5}, "from a sample of 0 rows, planned in 5.000 ms"),
        (
            {"sample_rows": 7, "planning_ms": 12.3456},
            "from a sample of 7 rows, planned in 12.346 ms",
        ),
        ({"sample_rows": 7, "planning_ms": True}, "from a sample of 7 rows"),
        ({"sample_rows": 7, "planning_ms": "1"}, "from a sample of 7 rows"),
    ],
)
def test_the_report_of_a_plan_from_a_sample_says_how_big_it_was(source: object, line: str) -> None:
    lines = replace(plan(SECTION_1), source=source).report.splitlines()
    assert lines[1] == line
    assert lines[2].endswith("  predicate")


@pytest.mark.parametrize(
    "source", [None, [], {"planning_ms": 5}, {"sample_rows": "7"}, {"sample_rows": True}]
)
def test_only_a_plan_from_a_sample_has_the_sample_line(source: object) -> None:
    lines = replace(plan(SECTION_1), source=source).report.splitlines()
    assert lines[1].endswith("  predicate")


def test_the_report_names_the_strategy_auto_chose_and_why() -> None:
    choice = {"strategy": "sample", "reason": "statistics_stale", "message": "300 rows changed"}
    source = {"auto": choice, "sample_rows": 30012, "planning_ms": 5}
    lines = replace(plan(SECTION_1), source=source).report.splitlines()
    assert lines[1:4] == [
        "strategy sample, chosen by auto: 300 rows changed",
        "from a sample of 30012 rows, planned in 5.000 ms",
        "rows  predicate",
    ]


@pytest.mark.parametrize(
    "auto",
    [
        None,
        "sample",
        {"strategy": "sample"},
        {"message": "why"},
        {"strategy": 1, "message": "why"},
        {"strategy": "sample", "message": None},
    ],
)
def test_only_a_choice_with_a_strategy_and_a_message_has_the_auto_line(auto: object) -> None:
    lines = replace(plan(SECTION_1), source={"auto": auto}).report.splitlines()
    assert lines[1].endswith("  predicate")


def test_to_json_is_to_dict_as_json_text() -> None:
    result = plan({**SECTION_1, "column": "größe", "source": {"table": "Straße"}})
    text = result.to_json()
    assert json.loads(text) == result.to_dict()
    assert text == json.dumps(result.to_dict(), ensure_ascii=False)
    assert '"column": "größe"' in text
    assert "\n" not in text
    assert result.to_json(indent=2) == json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


@pytest.mark.parametrize(
    ("source", "error"),
    [({"n_distinct": float("nan")}, ValueError), ({"at": object()}, TypeError)],
)
def test_to_json_refuses_a_source_json_cannot_hold(source: object, error: type[Exception]) -> None:
    result = replace(plan(SECTION_1), source=source)
    with pytest.raises(error):
        result.to_json()


PYSPARK_SECTION_1 = """\
# skewsplit: 4 of 4 partitions plus one for NULL, with an estimated skew score of 1.887
# spark, url, table and properties are yours: the session, the JDBC URL, the table,
# and the connection properties such as the user and password.
predicates = [
    '"customer_id" IS NULL',
    '"customer_id" < 42',
    '"customer_id" = 42',
    '"customer_id" > 42 AND "customer_id" < 520',
    '"customer_id" >= 520',
]
df = spark.read.jdbc(url, table, predicates=predicates, properties=properties)"""

SCALA_SECTION_1 = """\
// skewsplit: 4 of 4 partitions plus one for NULL, with an estimated skew score of 1.887
// spark, url, table and properties are yours: the session, the JDBC URL, the table,
// and the connection properties such as the user and password.
val predicates = Array(
  "\\"customer_id\\" IS NULL",
  "\\"customer_id\\" < 42",
  "\\"customer_id\\" = 42",
  "\\"customer_id\\" > 42 AND \\"customer_id\\" < 520",
  "\\"customer_id\\" >= 520"
)
val df = spark.read.jdbc(url, table, predicates, properties)"""

# Column names that a careless string literal gets wrong.
AWKWARD = [
    'say "hi"',
    "it's",
    "'''",
    "back\\slash\\",
    "\\u0022 is a quote in Scala 2.12",
    "\\\\u0022",
    "new\nline",
    "carriage\rreturn",
    "tab\tand\bbackspace\fform feed",
    "\x01\x1f\x7f\x85 controls",
    "\u2028\u2029 separators",
    "ü, 😀 and \U000e0001",
    "\ud800 lone surrogate",
    "$dollar ${x} %s {}",
]


def awkward_plan(column: str) -> Plan:
    return plan({**SECTION_1, "column": column})


def test_the_snippets_of_section_1() -> None:
    assert plan(SECTION_1).to_pyspark() == PYSPARK_SECTION_1
    assert plan(SECTION_1).to_scala() == SCALA_SECTION_1


def test_a_snippet_without_rows() -> None:
    empty = {"column": "c", "type": "int4", "partitions": 3, "null_rows": 0, "mcv": []}
    result = plan({**empty, "histogram": None})
    summary = "skewsplit: 1 of 3 partitions plus one for NULL, with no skew score"
    assert result.to_pyspark().split("\n")[0] == f"# {summary}"
    assert result.to_scala().split("\n")[0] == f"// {summary}"


@pytest.mark.parametrize("column", AWKWARD)
def test_the_comments_hold_nothing_from_the_column(column: str) -> None:
    result = awkward_plan(column)
    # Only the predicates differ, so a line break in a name cannot end a comment.
    for snippet, reference in [
        (result.to_pyspark(), PYSPARK_SECTION_1),
        (result.to_scala(), SCALA_SECTION_1),
    ]:
        assert snippet.split("\n")[:4] == reference.split("\n")[:4]
        assert snippet.split("\n")[-2:] == reference.split("\n")[-2:]


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def jdbc(self, url: str, table: str, *, predicates: list[str], properties: object) -> str:
        self.calls.append((url, table, predicates, properties))
        return "a data frame"


@pytest.mark.parametrize("column", AWKWARD)
def test_the_pyspark_snippet_runs_and_reads_with_the_predicates(column: str) -> None:
    result = awkward_plan(column)
    reader = FakeReader()
    spark = SimpleNamespace(read=reader)
    names: dict[str, object] = {"spark": spark, "url": "u", "table": "t", "properties": {}}
    exec(result.to_pyspark(), names)  # noqa: S102 -- the snippet under test, made here
    assert names["predicates"] == result.predicates
    assert names["df"] == "a data frame"
    assert reader.calls == [("u", "t", result.predicates, {})]
    # Every line of the code is printable, so pasting it shows what it does.
    assert all(line.isprintable() for line in result.to_pyspark().split("\n"))


def scala_unicode_escapes(source: str) -> str:
    """Do what Scala 2.12 does to source text before anything else.

    A backslash preceded in the source by an even number of backslashes, then
    one or more ``u`` and four hex digits, is replaced by that UTF-16 unit.
    """
    out: list[str] = []
    i = 0
    while i < len(source):
        backslashes = i - len(source[:i].rstrip("\\"))
        match = re.match(r"\\u+([0-9a-fA-F]{4})", source[i:])
        if match and backslashes % 2 == 0:
            out.append(chr(int(match.group(1), 16)))
            i += match.end()
        else:
            out.append(source[i])
            i += 1
    return "".join(out).encode("utf-16-be", "surrogatepass").decode("utf-16-be", "surrogatepass")


SCALA_NAMED = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "'": "'",
    "\\": "\\",
}


def scala_string(literal: str, *, unicode_escapes: bool) -> str:
    """Read one Scala string literal; Scala 2.13 and 3 also read \\u escapes in it."""
    assert literal[0] == literal[-1] == '"'
    body, out, i = literal[1:-1], [], 0
    while i < len(body):
        char = body[i]
        assert char not in '"\n\r', f"{char!r} ends the literal at {i}"
        if char != "\\":
            out.append(char)
            i += 1
        elif unicode_escapes and body[i + 1] == "u":
            out.append(chr(int(body[i + 2 : i + 6], 16)))
            i += 6
        else:
            out.append(SCALA_NAMED[body[i + 1]])
            i += 2
    text = "".join(out)
    return text.encode("utf-16-be", "surrogatepass").decode("utf-16-be", "surrogatepass")


def scala_literals(snippet: str) -> list[str]:
    lines = snippet.split("\n")
    start = lines.index("val predicates = Array(")
    end = lines.index(")", start)
    return [line.strip().removesuffix(",") for line in lines[start + 1 : end]]


@pytest.mark.parametrize("column", AWKWARD)
def test_the_scala_literals_read_back_in_scala_2_12_and_2_13(column: str) -> None:
    result = awkward_plan(column)
    snippet = result.to_scala()
    assert all(line.isprintable() for line in snippet.split("\n"))
    # Scala 2.13 and 3: \u is an escape inside a string.
    literals = scala_literals(snippet)
    assert [scala_string(literal, unicode_escapes=True) for literal in literals] == (
        result.predicates
    )
    # Scala 2.12: \u escapes are read first, everywhere; the literal must survive.
    literals = scala_literals(scala_unicode_escapes(snippet))
    assert [scala_string(literal, unicode_escapes=False) for literal in literals] == (
        result.predicates
    )


def test_the_scala_model_reads_unicode_escapes_like_scala_2_12() -> None:
    # The checks above are only as good as this model of Scala 2.12.
    assert scala_unicode_escapes("\\u0041 \\uuu0042") == "A B"
    assert scala_unicode_escapes("\\\\u0041") == "\\\\u0041"
    assert scala_unicode_escapes("\\\\\\u0041") == "\\\\A"
    assert scala_unicode_escapes('"\\u0022"') == '"""'
    assert scala_unicode_escapes("\\ud83d\\ude00") == "😀"
    # The backslashes are counted in the source, not in what escapes became.
    assert scala_unicode_escapes("\\u005c\\u0041") == "\\A"
