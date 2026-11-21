"""Run every golden fixture in spec/fixtures/ and its subdirectories against the core.

The output must equal the fixture's ``expected`` object exactly, apart from
the ``message`` of warnings and errors, whose wording is not part of the
contract (spec/README.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skewsplit.core.codecs import codec_for
from skewsplit.core.errors import SpecError
from skewsplit.core.pgarray import parse_values
from skewsplit.core.pgstats import from_pg_stats
from skewsplit.core.plan import SPEC_VERSION, error_dict
from skewsplit.core.planner import plan

pytestmark = pytest.mark.conformance

SPEC = Path(__file__).parents[3] / "spec"
FIXTURES = sorted((SPEC / "fixtures").glob("*.json"))
PG_ARRAY = sorted((SPEC / "fixtures" / "pg-array").glob("*.json"))
PG_STATS = sorted((SPEC / "fixtures" / "pg-stats").glob("*.json"))


def run(data: object) -> dict[str, Any]:
    """Return the output for an input as a JSON round trip: a plan or an error."""
    try:
        out = plan(data).to_dict()
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


def convert(data: object) -> dict[str, Any]:
    """Return the result of converting statistics (spec section 13): an input or an error."""
    try:
        out: object = {"spec_version": SPEC_VERSION, "input": from_pg_stats(data)}
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


def test_there_are_fixtures() -> None:
    assert len(FIXTURES) >= 12


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
