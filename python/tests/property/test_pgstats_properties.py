"""Fuzz the statistics conversion of spec section 13.

The reference computations here use Decimal and add up the buckets of
section 3 one by one, so they share no code with the conversion.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from skewsplit.core.errors import SpecError
from skewsplit.core.pgstats import from_pg_stats

pytestmark = pytest.mark.conformance

WHOLE = st.integers(0, 10**6).map(str)
DIGITS = st.text("0123456789", min_size=1, max_size=6)
EXPONENT = st.integers(-12, 12).map(lambda e: f"e{'-' if e < 0 else '+'}{abs(e):02d}")


@st.composite
def decimal_text(draw: st.DrawFn) -> str:
    """A text in the number form of section 13, such as 12.5, 3e-05 or 1.25e+07."""
    text = draw(WHOLE)
    if draw(st.booleans()):
        text += "." + draw(DIGITS)
    if draw(st.booleans()):
        text += draw(EXPONENT)
    return text


def exact(text: str) -> Decimal:
    return Decimal(text)


FRACTION = decimal_text().filter(lambda text: exact(text) <= 1)
RELTUPLES = decimal_text().filter(lambda text: exact(text) <= 10**12)


def rounded(value: Decimal) -> int:
    """floor(value + 1/2), with enough precision to be exact for these texts."""
    with localcontext() as context:
        context.prec = 200
        return int((value + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR))


def kept_rows(rows: int, buckets: int, lead: int, trail: int) -> int:
    """Add up the rows of buckets lead + 1 to buckets - trail, one by one (section 3)."""
    return sum(
        i * rows // buckets - (i - 1) * rows // buckets
        for i in range(lead + 1, buckets - trail + 1)
    )


@st.composite
def int4_statistics(draw: st.DrawFn) -> dict[str, Any]:
    values = draw(st.lists(st.integers(-1000, 1000), unique=True, max_size=8))
    others = st.integers(-1000, 1000).filter(lambda v: v not in values)
    bounds = sorted(draw(st.lists(others, min_size=2, max_size=12)))
    freqs = draw(st.lists(FRACTION, min_size=len(values), max_size=len(values)))
    has_histogram = draw(st.booleans())
    return {
        "column": "c",
        "type": "int4",
        "partitions": draw(st.integers(1, 50)),
        "reltuples": draw(st.lists(RELTUPLES, min_size=1, max_size=3)),
        "null_frac": draw(FRACTION),
        "most_common_vals": "{" + ",".join(map(str, values)) + "}" if values else None,
        "most_common_freqs": "{" + ",".join(freqs) + "}" if values else None,
        "histogram_bounds": "{" + ",".join(map(str, bounds)) + "}" if has_histogram else None,
        "modified_since_analyze": draw(st.none() | st.integers(0, 10**13)),
    }


@given(int4_statistics())
def test_row_counts_are_exact(stats: dict[str, Any]) -> None:
    result = from_pg_stats(stats)
    total = sum((exact(text) for text in stats["reltuples"]), Decimal(0))
    rows_estimate = rounded(total)
    null_rows = rounded(exact(stats["null_frac"]) * total)
    freqs = stats["most_common_freqs"][1:-1].split(",") if stats["most_common_freqs"] else []
    common = [max(1, rounded(exact(f) * total)) for f in freqs]
    assert result["null_rows"] == null_rows
    mcv: Any = result["mcv"]
    assert [entry["rows"] for entry in mcv] == common
    histogram: Any = result["histogram"]
    if stats["histogram_bounds"] is None:
        assert histogram is None
    else:
        assert histogram["rows"] == max(0, rows_estimate - null_rows - sum(common))
    source: Any = result["source"]
    assert source["rows_estimate"] == rows_estimate
    assert source["unestimated_rows"] == 0
    modified = stats["modified_since_analyze"]
    stale = modified is not None and modified * 10 > rows_estimate
    assert [w["code"] for w in source["warnings"]] == (["stale_statistics"] if stale else [])


DATES = st.dates(dt.date(1, 1, 1), dt.date(9999, 12, 31))


@st.composite
def date_statistics(draw: st.DrawFn) -> tuple[dict[str, Any], int, int]:
    """Date statistics whose histogram starts with lead -infinity and ends with trail infinity."""
    lead, trail = draw(st.integers(0, 3)), draw(st.integers(0, 3))
    dates = sorted(draw(st.lists(DATES, max_size=8)))
    bounds = ["-infinity"] * lead + [d.isoformat() for d in dates] + ["infinity"] * trail
    if len(bounds) < 2:
        bounds = ["-infinity", "infinity"]
        lead, trail = 2, 0
    special = draw(st.lists(st.sampled_from(["infinity", "-infinity"]), unique=True))
    freqs = draw(st.lists(FRACTION, min_size=len(special), max_size=len(special)))
    stats = {
        "column": "d",
        "type": "date",
        "partitions": 4,
        "reltuples": [draw(RELTUPLES)],
        "null_frac": draw(FRACTION),
        "most_common_vals": "{" + ",".join(special) + "}",
        "most_common_freqs": "{" + ",".join(freqs) + "}",
        "histogram_bounds": "{" + ",".join(bounds) + "}",
        "modified_since_analyze": None,
    }
    return stats, lead, trail


@given(date_statistics())
def test_special_values_are_left_out_with_their_rows(case: tuple[dict[str, Any], int, int]) -> None:
    stats, lead, trail = case
    result = from_pg_stats(stats)
    total = exact(stats["reltuples"][0])
    rows_estimate = rounded(total)
    null_rows = rounded(exact(stats["null_frac"]) * total)
    freqs = (
        stats["most_common_freqs"][1:-1].split(",") if stats["most_common_freqs"] != "{}" else []
    )
    common = [max(1, rounded(exact(f) * total)) for f in freqs]
    histogram_rows = max(0, rows_estimate - null_rows - sum(common))
    buckets = stats["histogram_bounds"].count(",")
    remaining = buckets + 1 - lead - trail
    kept = kept_rows(histogram_rows, buckets, lead, trail) if remaining >= 2 else 0
    assert result["mcv"] == []
    histogram: Any = result["histogram"]
    if remaining >= 2:
        assert histogram["rows"] == kept
        assert len(histogram["bounds"]) == remaining
    else:
        assert histogram is None
    source: Any = result["source"]
    unestimated = sum(common) + histogram_rows - kept
    assert source["unestimated_rows"] == unestimated
    codes = [w["code"] for w in source["warnings"]]
    assert codes == (["special_values_left_out"] if unestimated else [])


CODES = {
    "invalid_input",
    "invalid_statistics",
    "invalid_array",
    "invalid_value",
    "value_out_of_range",
    "unsupported_type",
    "invalid_partitions",
    "invalid_column",
    "invalid_rows",
    "duplicate_value",
    "invalid_histogram",
}


def outcome(stats: dict[str, Any]) -> dict[str, object] | str:
    """The converted input, or the error code."""
    try:
        return from_pg_stats(stats)
    except SpecError as error:
        return error.code


TEXT = st.text(alphabet='{}0123456789.e+-,"NaInfity ', max_size=12)
FIELD = st.none() | TEXT | st.integers(-5, 5) | st.lists(TEXT, max_size=2)


@given(
    st.fixed_dictionaries(
        {
            "column": st.sampled_from(["c", ""]),
            "type": st.sampled_from(["int4", "date", "numeric", "text"]),
            "partitions": st.integers(0, 3),
            "reltuples": st.lists(TEXT, max_size=2) | FIELD,
            "null_frac": TEXT | FIELD,
            "most_common_vals": FIELD,
            "most_common_freqs": FIELD,
            "histogram_bounds": FIELD,
            "modified_since_analyze": FIELD,
        },
        optional={"scale": st.integers(-1, 3) | TEXT, "extra": st.just(1)},
    )
)
def test_any_statistics_give_an_input_or_a_spec_error(stats: dict[str, Any]) -> None:
    result = outcome(stats)
    if isinstance(result, str):
        assert result in CODES
    else:
        assert set(result) >= {"column", "type", "partitions", "null_rows", "mcv", "histogram"}
