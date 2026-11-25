"""The Rust command line against the Python core, on random inputs.

Each input goes to the Python planner and, as JSON on standard input, to the
Rust binary's ``skewsplit plan --stats -``, in every ``--format``. Both must
print the same, byte for byte, with the same exit code. Spec section 9 leaves
the wording of errors out of the contract, but the crate keeps Python's.

Most inputs are valid; the rest break one rule of spec section 1 or 2 at
random, so that the errors are compared too. ``make differential`` builds the
binary and passes its path in ``SKEWSPLIT_RUST_BIN``. When the outputs
differ, Hypothesis shrinks the input, and the smallest one is written to
``reports/differential/difference.json`` in the shape of a fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from skewsplit.cli import _PLAN_FORMATS, INVALID
from skewsplit.core.codecs import codec_for
from skewsplit.core.errors import SpecError
from skewsplit.core.plan import error_dict
from skewsplit.core.planner import plan

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).parents[3]
DIFFERENCE = ROOT / "reports" / "differential" / "difference.json"
TYPES = ("int2", "int4", "int8", "numeric", "date", "timestamp", "timestamptz")
# Names that need quoting, and the first and last character of the ranges
# that Python's repr escapes.
COLUMNS = ("c", "customer_id", 'a"b', "Order Id", "select", "a\\b", "\x7f\x9f", "\u202e")


class Printed(NamedTuple):
    """What a run of the command line printed, and its exit code."""

    code: int
    stdout: str
    stderr: str


def rust_binary() -> str:
    """The Rust binary that ``make differential`` built."""
    binary = os.environ.get("SKEWSPLIT_RUST_BIN")
    if not binary:
        pytest.fail("set SKEWSPLIT_RUST_BIN to the Rust binary, or run make differential")
    return binary


def python_prints(data: object, form: str) -> Printed:
    """What the Rust command line must print for ``data``: the Python core's output."""
    try:
        result = plan(data)
    except SpecError as error:
        return Printed(INVALID, "", f"skewsplit: {error.code}: {error.message}\n")
    try:
        output = _PLAN_FORMATS[form](result)
    except ValueError as error:  # a NaN or an infinity in the source
        return Printed(INVALID, "", f"skewsplit: cannot write the plan as JSON: {error}\n")
    return Printed(0, output + "\n", "")


def rust_prints(stdin: str, form: str, partitions: int | None) -> Printed:
    """Run ``skewsplit plan --stats -`` with ``stdin`` on standard input."""
    args = [rust_binary(), "plan", "--stats", "-", "--format", form]
    if partitions is not None:
        args += ["--partitions", str(partitions)]
    run = subprocess.run(  # noqa: S603 -- the binary that make differential built
        args, input=stdin.encode(), capture_output=True, check=False, timeout=60
    )
    return Printed(run.returncode, run.stdout.decode(), run.stderr.decode())


def expected(data: object) -> object:
    """The ``expected`` of a planner fixture: the Python core's plan or error."""
    try:
        return plan(data).to_dict()
    except SpecError as error:
        return error_dict(error)
    except Exception:  # noqa: BLE001 -- the difference shows how the core failed
        return None


# Text with no unassigned code point, here and in every value below: Python's
# answer for those depends on its Unicode version, and the crate calls them
# printable, so its messages and snippets would differ by design.
text = st.text(st.characters(exclude_categories=("Cn", "Cs")), max_size=12)
# Any JSON value, for the fields that get one at random.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | text,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(text, children, max_size=3),
    max_leaves=6,
)
numbers = (
    st.integers(-2, 10**4)
    | st.integers()
    | st.sampled_from([2**63 - 1, 2**63, -(2**63), 10**400])
    | st.floats()
    | st.sampled_from([0.0, -0.0, 0.0005, 0.0015, 2.5])
)


@st.composite
def sources(draw: st.DrawFn) -> object:
    """A source as the adapters write it, with fields that may be odd."""
    source: dict[str, Any] = {}
    if draw(st.booleans()):
        source["auto"] = draw(
            st.fixed_dictionaries({"strategy": text, "message": text}) | json_values
        )
    if draw(st.booleans()):
        source["sample_rows"] = draw(numbers | st.booleans())
        if draw(st.booleans()):
            source["planning_ms"] = draw(numbers | st.booleans() | json_values)
    if draw(st.booleans()):
        warning = st.fixed_dictionaries({"code": text, "message": text}) | json_values
        source["warnings"] = draw(st.lists(warning, max_size=3) | json_values)
    if draw(st.booleans()):
        source.update(draw(st.dictionaries(text, json_values, max_size=2)))
    return draw(st.just(source) | json_values)


@st.composite
def valid_inputs(draw: st.DrawFn) -> dict[str, Any]:
    """A valid input, with values in a window of the type that may reach its limits."""
    type_name = draw(st.sampled_from(TYPES))
    scales = st.integers(0, 3) | st.sampled_from([18, 1000])
    scale = draw(scales) if type_name == "numeric" else None
    codec = codec_for(type_name, scale)
    width = draw(st.sampled_from([10, 1000, 10**9, 2**70]))
    width = min(width, codec.max_ordinal - codec.min_ordinal)
    start = draw(st.integers(codec.min_ordinal, codec.max_ordinal - width))
    if draw(st.booleans()):
        start = draw(st.sampled_from([codec.min_ordinal, codec.max_ordinal - width]))
    pool = sorted(draw(st.sets(st.integers(start, start + width), max_size=12)))
    bounds = sorted(draw(st.lists(st.sampled_from(pool), min_size=2, max_size=10))) if pool else []
    if len(bounds) < 2 or draw(st.booleans()):
        bounds = []
    values = [v for v in pool if v not in bounds and draw(st.booleans())]
    rows = st.integers(1, 100) | st.integers(1, 10**12) | st.integers(10**13, 10**15)
    data: dict[str, Any] = {"column": draw(st.sampled_from(COLUMNS) | text), "type": type_name}
    if scale is not None:
        data["scale"] = scale
    data["partitions"] = draw(st.integers(1, 12) | st.integers(1, 10000))
    data["null_rows"] = draw(st.just(0) | st.integers(0, 10**6))
    data["mcv"] = [{"value": codec.text(v), "rows": draw(rows)} for v in values]
    data["histogram"] = (
        {"bounds": [codec.text(b) for b in bounds], "rows": draw(st.integers(0, 10**12))}
        if bounds
        else None
    )
    if draw(st.booleans()):
        data["source"] = draw(sources())
    return data


# A value that a text form or a row count must refuse, or one at its limit.
odd_values = (
    st.sampled_from(
        ["", "-0", "01", "+1", " 1", "1.", ".5", "1e3", "NaN", "2026-02-30", "2026-11-18 24:00:00"]
    )
    | text
)


def _beyond_range(draw: st.DrawFn, data: dict[str, Any]) -> None:
    # Only integers and numerics can be out of range: a date outside years 1
    # to 9999 is not in the text form at all.
    if data["type"] not in ("int2", "int4", "int8", "numeric"):
        return
    codec = codec_for(data["type"], data.get("scale"))
    past = draw(st.integers(1, 3) | st.integers(1, 10**30))
    ordinal = codec.max_ordinal + past if draw(st.booleans()) else codec.min_ordinal - past
    scale = data.get("scale") or 0
    whole, fraction = divmod(abs(ordinal), 10**scale)
    value = ("-" if ordinal < 0 else "") + str(whole) + (f".{fraction:0{scale}d}" if scale else "")
    if data["histogram"] and draw(st.booleans()):
        bounds = data["histogram"]["bounds"]
        bounds[draw(st.integers(0, len(bounds) - 1))] = value
    elif data["mcv"]:
        draw(st.sampled_from(data["mcv"]))["value"] = value
    else:
        data["mcv"].append({"value": value, "rows": 1})


def _break_field(draw: st.DrawFn, data: dict[str, Any]) -> None:
    name = draw(st.sampled_from(sorted(data)))
    if draw(st.booleans()):
        del data[name]
    else:
        data[name] = draw(json_values)


def _odd_number(draw: st.DrawFn, data: dict[str, Any]) -> None:
    name = draw(st.sampled_from(["partitions", "null_rows", "scale"]))
    data[name] = draw(numbers | st.sampled_from([0, -1, 10000, 10001, 1001, True, "4"]))


def _odd_value(draw: st.DrawFn, data: dict[str, Any]) -> None:
    if data["mcv"]:
        draw(st.sampled_from(data["mcv"]))["value"] = draw(odd_values)
    if data["histogram"] and draw(st.booleans()):
        bounds = data["histogram"]["bounds"]
        bounds[draw(st.integers(0, len(bounds) - 1))] = draw(odd_values)


def _odd_rows(draw: st.DrawFn, data: dict[str, Any]) -> None:
    rows = st.sampled_from([0, -1, 2**63 - 1, 2**63]) | numbers
    for entry in data["mcv"]:
        if draw(st.booleans()):
            entry["rows"] = draw(rows)
    if data["histogram"]:
        data["histogram"]["rows"] = draw(rows)


def _odd_histogram(draw: st.DrawFn, data: dict[str, Any]) -> None:
    values = [entry["value"] for entry in data["mcv"]]
    bounds = draw(st.lists(st.sampled_from(values) | odd_values, max_size=4)) if values else []
    data["histogram"] = draw(
        st.just({"bounds": bounds, "rows": 10})
        | st.just({"bounds": list(reversed(bounds)), "rows": 10})
        | json_values
    )


def _duplicate_value(draw: st.DrawFn, data: dict[str, Any]) -> None:
    if data["mcv"]:
        data["mcv"].append(dict(draw(st.sampled_from(data["mcv"]))))


def _odd_type(draw: st.DrawFn, data: dict[str, Any]) -> None:
    data["type"] = draw(st.sampled_from([*TYPES, "text", "INT4", "float8"]) | text)


def _unknown_field(draw: st.DrawFn, data: dict[str, Any]) -> None:
    data[draw(text)] = draw(json_values)


# In this order, each break finds the fields that the ones before it left:
# the values first, then the histogram, then any field at all. The first
# needs the type and scale still valid.
BREAKS: tuple[Callable[[st.DrawFn, dict[str, Any]], None], ...] = (
    _beyond_range,
    _odd_number,
    _odd_value,
    _odd_rows,
    _duplicate_value,
    _odd_type,
    _odd_histogram,
    _break_field,
    _unknown_field,
)


@st.composite
def inputs(draw: st.DrawFn) -> object:
    """A valid input, one with a few rules broken, or a JSON value that is no input."""
    data: Any = draw(valid_inputs())
    if draw(st.booleans()):  # half of the inputs keep to the rules
        return data
    for index in sorted(draw(st.sets(st.integers(0, len(BREAKS) - 1), min_size=1, max_size=2))):
        BREAKS[index](draw, data)
    if not draw(st.integers(0, 50)):
        data = draw(json_values)
    return data


def write_difference(data: object, outputs: Mapping[str, object]) -> None:
    """Write the input in the shape of a planner fixture, with what each side printed."""
    DIFFERENCE.parent.mkdir(parents=True, exist_ok=True)
    fixture = {
        "description": "The Rust command line prints something else than Python.",
        "input": data,
        "expected": expected(data),
        "outputs": outputs,
    }
    DIFFERENCE.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")


@given(data=inputs(), ascii_only=st.booleans(), option=st.booleans())
def test_the_rust_command_line_prints_what_python_prints(
    *, data: object, ascii_only: bool, option: bool
) -> None:
    # --partitions on the command line adds the field. The crate reads it as
    # an i128, which holds every number the planner could take.
    partitions = None
    if option and isinstance(data, dict):
        number = data.get("partitions")
        if type(number) is int and abs(number) < 2**100:
            partitions = data.pop("partitions")
    stdin = json.dumps(data, ensure_ascii=ascii_only)
    # Python reads the same text as the crate.
    decoded: Any = json.loads(stdin)
    if partitions is not None:
        decoded["partitions"] = partitions
    try:
        python = {form: python_prints(decoded, form) for form in _PLAN_FORMATS}
    except Exception as error:
        write_difference(decoded, {"python": f"{type(error).__name__}: {error}"})
        raise
    rust = {form: rust_prints(stdin, form, partitions) for form in _PLAN_FORMATS}
    event(python["text"].stderr.split(":")[1].strip() if python["text"].code else "plan")
    differ = {
        form: {"python": python[form]._asdict(), "rust": rust[form]._asdict()}
        for form in _PLAN_FORMATS
        if python[form] != rust[form]
    }
    if differ:
        write_difference(decoded, differ)
    assert not differ, json.dumps(differ, indent=2)
