"""Plan balanced range predicates from a column's distribution (spec sections 3 to 8).

>>> from skewsplit.core.planner import plan
>>> result = plan(
...     {
...         "column": "id",
...         "type": "int4",
...         "partitions": 2,
...         "null_rows": 0,
...         "mcv": [],
...         "histogram": {"bounds": ["1", "101"], "rows": 100},
...     }
... )
>>> [p.sql for p in result.predicates]
['"id" IS NULL', '"id" < 51', '"id" >= 51']
"""

from __future__ import annotations

import heapq
from bisect import bisect_left
from fractions import Fraction
from itertools import accumulate
from typing import TYPE_CHECKING

from skewsplit.core.inputs import PlanInput, parse_input
from skewsplit.core.plan import Bound, Plan, PlanWarning, Predicate

if TYPE_CHECKING:
    from skewsplit.core.codecs import Codec


class Estimate:
    """The estimate model ``G`` of spec section 3, without the dominant values.

    ``below(x)`` is the number of rows that are not NULL, not a dominant value,
    and lie below ``x``. It takes O(log n) time: prefix sums over the common
    values and the buckets, and a binary search for ``x``.
    """

    def __init__(self, inp: PlanInput, dominant: frozenset[int]) -> None:
        """Build the model for an input whose ``dominant`` ordinals are excluded."""
        rest = [(value, rows) for value, rows in inp.mcv if value not in dominant]
        self._values = [value for value, _ in rest]
        self._value_rows = [0, *accumulate(rows for _, rows in rest)]
        self._bounds = list(inp.bounds)
        buckets = max(len(self._bounds) - 1, 0)
        h = inp.histogram_rows
        # Bucket i (1 to m) holds i*H/m - (i-1)*H/m rows, so the first i hold i*H/m.
        self._bucket_rows = [i * h // buckets for i in range(buckets + 1)] if buckets else [0]
        self.top = self._value_rows[-1] + self._bucket_rows[-1]

    def below(self, x: int) -> int:
        """Return ``G(x)``."""
        values = self._value_rows[bisect_left(self._values, x)]
        return values + self._histogram_below(x)

    def _histogram_below(self, x: int) -> int:
        bounds, cumulative = self._bounds, self._bucket_rows
        # Bounds before index p are below x, so buckets 1 to p - 1 lie wholly below
        # it. Bucket p ends at bounds[p]: whole if that bound is x (as lo < hi = x),
        # otherwise lo < x < hi and the rows below x are interpolated.
        p = bisect_left(bounds, x)
        if p == 0:
            return 0
        if p == len(bounds):
            return cumulative[-1]
        rows = cumulative[p] - cumulative[p - 1]
        low, high = bounds[p - 1], bounds[p]
        partial = rows if high == x else rows * (x - low) // (high - low)
        return cumulative[p - 1] + partial

    def at(self, limit: int | None, *, upper: bool) -> int:
        """Return ``G`` at a segment limit, where ``None`` is -∞ or, if ``upper``, +∞."""
        if limit is None:
            return self.top if upper else 0
        return self.below(limit)


def plan(data: object) -> Plan:
    """Check a decoded JSON input and plan it.

    Raises:
        SpecError: If the input is invalid (spec section 9).
    """
    return plan_input(parse_input(data))


def plan_input(inp: PlanInput) -> Plan:
    """Plan a checked input."""
    n = inp.partitions
    non_null = sum(rows for _, rows in inp.mcv) + inp.histogram_rows
    total = inp.null_rows + non_null
    codec = inp.codec

    # 4.1: candidates hold at least one partition's share; the largest
    # (N - 1) / 2, smallest ordinal first on equal rows, are isolated.
    candidates = sorted(
        ((value, rows) for value, rows in inp.mcv if rows * n >= non_null),
        key=lambda item: (-item[1], item[0]),
    )
    isolated = (n - 1) // 2
    dominant = sorted(candidates[:isolated])
    not_isolated = sorted(candidates[isolated:])
    g = Estimate(inp, frozenset(value for value, _ in dominant))

    # 4.2: segments between the dominant values share the N - D ranges.
    limits: list[int | None] = [None, *(value for value, _ in dominant), None]
    segments = [_Segment(g, lower, upper) for lower, upper in zip(limits, limits[1:])]
    counts = _allocate([segment.rows for segment in segments], n - len(dominant))

    quoted = '"' + inp.column.replace('"', '""') + '"'
    predicates = [Predicate("null", f"{quoted} IS NULL", inp.null_rows)]
    for j, segment in enumerate(segments):
        cuts = [segment.lower, *_split_points(g, codec, segment, counts[j]), segment.upper]
        for k, (start, end) in enumerate(zip(cuts, cuts[1:])):
            low = None if start is None else Bound(">" if k == 0 else ">=", codec.text(start))
            high = None if end is None else Bound("<", codec.text(end))
            parts = [
                f"{quoted} {bound.op} {codec.literal(ordinal)}"
                for bound, ordinal in ((low, start), (high, end))
                if bound is not None and ordinal is not None
            ]
            sql = " AND ".join(parts) or f"{quoted} IS NOT NULL"
            rows = g.at(end, upper=True) - g.at(start, upper=False)
            predicates.append(Predicate("range", sql, rows, lower=low, upper=high))
        if j < len(dominant):
            value, rows = dominant[j]
            literal = codec.literal(value)
            predicates.append(
                Predicate("value", f"{quoted} = {literal}", rows, value=codec.text(value))
            )

    warnings = _warnings(inp, dominant, not_isolated, len(predicates) - 1, total)
    largest = max(predicate.estimated_rows for predicate in predicates)
    skew = None if total == 0 else (2 * largest * n * 1000 + total) // (2 * total)
    return Plan(
        column=inp.column,
        type_name=codec.type_name,
        scale=getattr(codec, "scale", None),
        requested_partitions=n,
        null_rows=inp.null_rows,
        total_rows=total,
        predicates=tuple(predicates),
        skew_milli=skew,
        warnings=tuple(warnings),
        source=inp.source,
    )


def _allocate(sizes: list[int], ranges: int) -> list[int]:
    """Give each segment one range, then each extra range to the largest share (4.2).

    The share of segment j is sizes[j] / counts[j]; ties go to the lowest segment.
    """
    counts = [1] * len(sizes)
    heap = [(-Fraction(size), j) for j, size in enumerate(sizes)]
    heapq.heapify(heap)
    for _ in range(ranges - len(sizes)):
        _, j = heapq.heappop(heap)
        counts[j] += 1
        heapq.heappush(heap, (-Fraction(sizes[j], counts[j]), j))
    return counts


class _Segment:
    """The ordinals between two neighbouring limits, both excluded (4.2).

    ``lower`` is a dominant value or ``None`` for -∞; ``upper`` a dominant value
    or ``None`` for +∞. ``base`` and ``top`` are ``G`` at the two limits.
    """

    def __init__(self, g: Estimate, lower: int | None, upper: int | None) -> None:
        self.lower, self.upper = lower, upper
        self.base = g.at(lower, upper=False)
        self.top = g.at(upper, upper=True)
        self.rows = self.top - self.base


def _split_points(g: Estimate, codec: Codec, segment: _Segment, count: int) -> list[int]:
    """Return the split points of a segment that has ``count`` ranges (4.3)."""
    lower, base, top = segment.lower, segment.base, segment.top
    max_ordinal = codec.max_ordinal
    first = codec.min_ordinal if lower is None else lower + 1
    points: list[int] = []
    previous = base
    for q in range(1, count):
        target = q * segment.rows // count
        if target == 0:
            continue
        # x_hi: the smallest ordinal from `first` with G(x) - G(L) >= target. It is
        # at most MAX + 1, as the segment holds more than `target` rows.
        lo, hi = first, max_ordinal + 1
        while lo < hi:
            mid = (lo + hi) // 2
            if g.below(mid) - base >= target:
                hi = mid
            else:
                lo = mid + 1
        x_hi, x_lo = lo, lo - 1
        options = []
        if x_hi <= max_ordinal:
            options.append((g.below(x_hi) - base - target, x_hi))
        if lower is None or x_lo > lower:
            options.append((target - (g.below(x_lo) - base), x_lo))
        # Nearer to the target wins; on a tie, x_hi, which is listed first.
        _, point = min(options, key=lambda option: option[0])
        rows = g.below(point)
        if previous < rows < top:
            points.append(point)
            previous = rows
    return points


def _warnings(
    inp: PlanInput,
    dominant: list[tuple[int, int]],
    not_isolated: list[tuple[int, int]],
    planned: int,
    total: int,
) -> list[PlanWarning]:
    """Return the warnings of spec section 8, in its order."""
    text, n = inp.codec.text, inp.partitions
    warnings = [
        PlanWarning(
            "dominant_value",
            f"{text(value)} holds {rows} rows, at least one partition's share; "
            "it cannot be split further on this column",
            value=text(value),
            rows=rows,
        )
        for value, rows in dominant
    ]
    warnings += [
        PlanWarning(
            "dominant_value_not_isolated",
            f"{text(value)} holds {rows} rows, at least one partition's share, but {n} "
            "partitions are too few to give it its own predicate",
            value=text(value),
            rows=rows,
        )
        for value, rows in not_isolated
    ]
    if planned < n:
        message = (
            f"planned {planned} of the {n} requested partitions: the column has too few "
            "distinct values, or too many rows at single values"
        )
        warnings.append(PlanWarning("fewer_partitions", message, requested=n, planned=planned))
    if inp.null_rows * n > total:
        message = f"{inp.null_rows} NULL rows are more than one partition's share"
        warnings.append(PlanWarning("nulls_exceed_share", message, rows=inp.null_rows))
    if total == 0:
        message = "the statistics show no rows; the plan still reads rows added later"
        warnings.append(PlanWarning("no_rows", message))
    return warnings
