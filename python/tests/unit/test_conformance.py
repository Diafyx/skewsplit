"""Run every golden fixture in spec/fixtures/ against the Python core.

The output must equal the fixture's ``expected`` object exactly, apart from
the ``message`` of warnings and errors, whose wording is not part of the
contract (spec/README.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skewsplit.core.errors import SpecError
from skewsplit.core.plan import error_json
from skewsplit.core.planner import plan

pytestmark = pytest.mark.conformance

FIXTURES = sorted((Path(__file__).parents[3] / "spec" / "fixtures").glob("*.json"))


def run(data: object) -> dict[str, Any]:
    """Return the output for an input as a JSON round trip: a plan or an error."""
    try:
        out = plan(data).to_json()
    except SpecError as error:
        out = error_json(error)
    decoded: dict[str, Any] = json.loads(json.dumps(out, allow_nan=False))
    return decoded


def without_messages(out: dict[str, Any]) -> dict[str, Any]:
    """Remove the messages, after checking that each one is a non-empty string."""
    entries = out["warnings"] if "warnings" in out else [out["error"]]
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
