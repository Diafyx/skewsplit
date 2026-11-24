"""Run every golden fixture in spec/fixtures/ and its subdirectories against the core.

The output must equal the fixture's ``expected`` object exactly, apart from
the ``message`` of warnings and errors, whose wording is not part of the
contract (spec/README.md).

What ``skewsplit plan`` prints for each planner input of the fixtures, in each
``--format``, is in rust/tests/cli/python-outputs.json, and the Rust command
line must print it byte for byte (rust/tests/fixtures.rs). After a change to
the output, write the file again with ``SKEWSPLIT_WRITE_CLI_OUTPUTS=1``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest

from skewsplit.cli import _PLAN_FORMATS
from skewsplit.core.codecs import codec_for
from skewsplit.core.errors import SpecError
from skewsplit.core.hashing import hash_plan
from skewsplit.core.pgarray import parse_values
from skewsplit.core.pgstats import from_pg_stats
from skewsplit.core.plan import SPEC_VERSION, Plan, error_dict
from skewsplit.core.planner import plan
from skewsplit.core.sample import from_sample

pytestmark = pytest.mark.conformance

# The nearest spec/ above, so a copy of the tests one folder deeper, as make
# mutation runs them, finds it too.
SPEC = next(path for path in Path(__file__).parents if (path / "spec").is_dir()) / "spec"
FIXTURES = sorted((SPEC / "fixtures").glob("*.json"))
PG_ARRAY = sorted((SPEC / "fixtures" / "pg-array").glob("*.json"))
PG_STATS = sorted((SPEC / "fixtures" / "pg-stats").glob("*.json"))
SAMPLE = sorted((SPEC / "fixtures" / "sample").glob("*.json"))
HASH = sorted((SPEC / "fixtures" / "hash").glob("*.json"))
CLI_OUTPUTS = SPEC.parent / "rust" / "tests" / "cli" / "python-outputs.json"

# The input of spec section 1, and column names that a careless string
# literal gets wrong, for the command line's output.
SECTION_1 = {
    "column": "customer_id",
    "type": "int4",
    "partitions": 4,
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400},
}
AWKWARD_COLUMNS = [
    'say "hi"',
    "it's",
    "'''",
    "back\\slash\\",
    "\\u0022 is a quote in Scala 2.12",
    "new\nline",
    "carriage\rreturn",
    "tab\tand\bbackspace\fform feed",
    "\x01\x1f\x7f\x85 controls",
    "\u00a0\u2028\u2029\u3000 spaces and separators",
    "\u00ad\u200b\u202e\u2066\ufeff format characters",
    "\ue000\U000f0000 private use",
    "ü, 😀 and \U000e0001",
    "$dollar ${x} %s {}",
    # The first and last character of each range that the crate's table of
    # characters repr escapes holds, apart from NUL, which no column holds.
    # Each is not printable in every Python, assigned there or not.
    "ends "
    + "".join(
        map(
            chr,
            [
                *(0x01, 0x1F, 0x7F, 0x9F, 0xA0, 0xAD, 0x600, 0x605, 0x61C, 0x6DD, 0x70F),
                *(0x890, 0x891, 0x8E2, 0x1680, 0x180E, 0x2000, 0x200F, 0x2028, 0x202F),
                *(0x205F, 0x2064, 0x2066, 0x206F, 0x3000, 0xE000, 0xF8FF, 0xFEFF, 0xFFF9),
                *(0xFFFB, 0x110BD, 0x110CD, 0x13430, 0x1343F, 0x1BCA0, 0x1BCA3, 0x1D173),
                *(0x1D17A, 0xE0001, 0xE0020, 0xE007F, 0xF0000, 0xFFFFD, 0x100000, 0x10FFFD),
            ],
        )
    ),
]


def run(data: object, planner: Callable[[object], Plan] = plan) -> dict[str, Any]:
    """Return the output for an input as a JSON round trip: a plan or an error."""
    try:
        out = planner(data).to_dict()
    except SpecError as error:
        out = error_dict(error)
    decoded: dict[str, Any] = json.loads(json.dumps(out, allow_nan=False))
    return decoded


def read_array(data: dict[str, Any]) -> dict[str, Any]:
    """Return the result of reading array text (spec section 12): values or an error."""
    try:
        codec = codec_for(data["type"], data.get("scale"))
        out: object = {"spec_version": SPEC_VERSION, "values": parse_values(data["text"], codec)}
    except SpecError as error:
        out = error_dict(error)
    decoded: dict[str, Any] = json.loads(json.dumps(out))
    return decoded


def convert(data: object, conversion: Callable[[object], object] = from_pg_stats) -> dict[str, Any]:
    """Return the result of converting statistics (spec section 13 or 14): an input or an error."""
    try:
        out: object = {"spec_version": SPEC_VERSION, "input": conversion(data)}
    except SpecError as error:
        out = error_dict(error)
    decoded: dict[str, Any] = json.loads(json.dumps(out, allow_nan=False))
    return decoded


def without_messages(out: dict[str, Any]) -> dict[str, Any]:
    """Remove the messages, after checking that each one is a non-empty string."""
    if "input" in out:
        entries = out["input"]["source"]["warnings"]
    elif "warnings" in out:
        entries = out["warnings"]
    else:
        entries = [out["error"]] if "error" in out else []
    for entry in entries:
        message = entry.pop("message")
        assert isinstance(message, str)
        assert message
    return out


def cli_outputs() -> dict[str, Any]:
    """What ``skewsplit plan`` prints for each planner input, without the last line break.

    The inputs are those of the planner fixtures, the planner inputs that the
    statistics and sample fixtures expect, and the input of spec section 1
    with each of ``AWKWARD_COLUMNS``. For an input that cannot be planned, the
    case has the error line after ``skewsplit: `` instead.
    """
    inputs = [
        (path.name, json.loads(path.read_text(encoding="utf-8"))["input"]) for path in FIXTURES
    ]
    for folder, paths in (("pg-stats", PG_STATS), ("sample", SAMPLE)):
        for path in paths:
            expected = json.loads(path.read_text(encoding="utf-8"))["expected"]
            if "input" in expected:
                inputs.append((f"{folder}/{path.name}", expected["input"]))
    for number, column in enumerate(AWKWARD_COLUMNS, 1):
        inputs.append((f"section-1-column-{number}", {**SECTION_1, "column": column}))
    # A source as postgres.plan writes it for auto (spec section 17), with
    # the time in milliseconds as a float and as an integer.
    auto = {"strategy": "sample", "message": "the statistics are older than the table's changes"}
    for name, planning_ms in (("float", 12.3456), ("integer", 12)):
        source = {"auto": auto, "sample_rows": 30000, "planning_ms": planning_ms}
        inputs.append((f"section-1-auto-{name}", {**SECTION_1, "source": source}))
    cases = []
    for name, data in inputs:
        case: dict[str, Any] = {"name": name, "input": data}
        try:
            result = plan(data)
        except SpecError as error:
            case["error"] = f"{error.code}: {error.message}"
        else:
            case.update({form: render(result) for form, render in _PLAN_FORMATS.items()})
        cases.append(case)
    return {"cases": cases}


def test_the_command_line_outputs_for_rust_are_up_to_date() -> None:
    text = json.dumps(cli_outputs(), indent=2) + "\n"
    if os.environ.get("SKEWSPLIT_WRITE_CLI_OUTPUTS") == "1":
        CLI_OUTPUTS.write_text(text, encoding="utf-8")
    message = f"write {CLI_OUTPUTS.name} again with SKEWSPLIT_WRITE_CLI_OUTPUTS=1"
    assert CLI_OUTPUTS.read_text(encoding="utf-8") == text, message


def test_there_are_fixtures() -> None:
    assert len(FIXTURES) >= 12


def test_every_fixture_folder_is_run() -> None:
    # A new folder fails this until its fixtures are run here.
    folders = {path.name for path in (SPEC / "fixtures").iterdir() if path.is_dir()}
    assert folders == {"hash", "pg-array", "pg-stats", "sample"}


@pytest.mark.parametrize("path", FIXTURES, ids=[path.stem for path in FIXTURES])
def test_fixture(path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    actual, expected = without_messages(run(fixture["input"])), fixture["expected"]
    assert actual == expected, fixture["description"]
    # == treats 1 and 1.0, or 1 and true, as equal; the JSON text does not.
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_there_are_array_text_fixtures_for_every_error_code() -> None:
    schema = json.loads((SPEC / "schema" / "pg-array.schema.json").read_text(encoding="utf-8"))
    error = schema["properties"]["expected"]["oneOf"][1]["properties"]["error"]
    codes = {
        json.loads(path.read_text(encoding="utf-8"))["expected"]["error"]["code"]
        for path in PG_ARRAY
        if path.name.startswith("error-")
    }
    assert codes == set(error["properties"]["code"]["enum"])
    assert len(PG_ARRAY) >= 30


@pytest.mark.parametrize("path", PG_ARRAY, ids=[path.stem for path in PG_ARRAY])
def test_array_text_fixture(path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    actual, expected = without_messages(read_array(fixture["input"])), fixture["expected"]
    assert actual == expected, fixture["description"]


def test_there_are_statistics_fixtures_for_every_error_code() -> None:
    schema = json.loads((SPEC / "schema" / "pg-stats.schema.json").read_text(encoding="utf-8"))
    codes = {
        json.loads(path.read_text(encoding="utf-8"))["expected"]["error"]["code"]
        for path in PG_STATS
        if path.name.startswith("error-")
    }
    assert codes == set(
        schema["$defs"]["error"]["properties"]["error"]["properties"]["code"]["enum"]
    )
    assert len(PG_STATS) >= 40


@pytest.mark.parametrize("path", PG_STATS, ids=[path.stem for path in PG_STATS])
def test_statistics_fixture(path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    actual, expected = without_messages(convert(fixture["input"])), fixture["expected"]
    assert actual == expected, fixture["description"]
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    if "input" in expected:
        # The result is a planner input: it plans, and the plan carries its source.
        assert run(expected["input"])["source"] == expected["input"]["source"]


def test_there_are_sample_fixtures_for_every_error_code() -> None:
    schema = json.loads((SPEC / "schema" / "sample.schema.json").read_text(encoding="utf-8"))
    codes = {
        json.loads(path.read_text(encoding="utf-8"))["expected"]["error"]["code"]
        for path in SAMPLE
        if path.name.startswith("error-")
    }
    assert codes == set(
        schema["$defs"]["error"]["properties"]["error"]["properties"]["code"]["enum"]
    )
    assert len(SAMPLE) >= 30


@pytest.mark.parametrize("path", SAMPLE, ids=[path.stem for path in SAMPLE])
def test_sample_fixture(path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    actual = without_messages(convert(fixture["input"], from_sample))
    expected = fixture["expected"]
    assert actual == expected, fixture["description"]
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    if "input" in expected:
        assert run(expected["input"])["source"] == expected["input"]["source"]


def test_there_are_hash_fixtures_for_every_error_code() -> None:
    schema = json.loads((SPEC / "schema" / "hash.schema.json").read_text(encoding="utf-8"))
    codes = {
        json.loads(path.read_text(encoding="utf-8"))["expected"]["error"]["code"]
        for path in HASH
        if path.name.startswith("error-")
    }
    assert codes == set(
        schema["$defs"]["error"]["properties"]["error"]["properties"]["code"]["enum"]
    )
    assert len(HASH) >= 20


@pytest.mark.parametrize("path", HASH, ids=[path.stem for path in HASH])
def test_hash_fixture(path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    actual = without_messages(run(fixture["input"], hash_plan))
    expected = fixture["expected"]
    assert actual == expected, fixture["description"]
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
