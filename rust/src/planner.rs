//! Plan balanced range predicates from a column's distribution (spec
//! sections 3 to 8).
//!
//! ```
//! use skewsplit::{json, planner::plan};
//!
//! let data = json::parse(
//!     r#"{"column": "id", "type": "int4", "partitions": 2, "null_rows": 0,
//!         "mcv": [], "histogram": {"bounds": ["1", "101"], "rows": 100}}"#,
//! )?;
//! let result = plan(&data)?;
//! assert_eq!(result.predicates(), [r#""id" IS NULL"#, r#""id" < 51"#, r#""id" >= 51"#]);
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```

use std::cmp::Ordering;
use std::collections::{BTreeSet, BinaryHeap};

use crate::codecs::Codec;
use crate::error::SpecError;
use crate::inputs::{PlanInput, parse_input};
use crate::json::Value;
use crate::plan::{Bound, Kind, Op, Plan, PlanWarning, Predicate, WarningCode};

/// Check a decoded JSON input and plan it.
///
/// # Errors
///
/// If the input is invalid (spec section 9).
pub fn plan(data: &Value) -> Result<Plan, SpecError> {
    Ok(plan_input(&parse_input(data)?))
}

/// Plan a checked input.
#[must_use]
pub fn plan_input(input: &PlanInput) -> Plan {
    let n = input.partitions();
    let non_null: i128 =
        input.mcv().iter().map(|&(_, rows)| rows).sum::<i128>() + input.histogram_rows();
    let total = input.null_rows() + non_null;
    let codec = input.codec();

    // 4.1: candidates hold at least one partition's share; the largest
    // (N - 1) / 2, smallest ordinal first on equal rows, are isolated.
    let mut candidates: Vec<(i128, i128)> = input
        .mcv()
        .iter()
        .copied()
        .filter(|&(_, rows)| rows * n >= non_null)
        .collect();
    candidates.sort_by_key(|&(value, rows)| (std::cmp::Reverse(rows), value));
    let isolated = usize::try_from((n - 1) / 2)
        .unwrap_or(usize::MAX)
        .min(candidates.len());
    let mut not_isolated = candidates.split_off(isolated);
    let mut dominant = candidates;
    dominant.sort_unstable();
    not_isolated.sort_unstable();
    let g = Estimate::new(input, &dominant);

    // 4.2: segments between the dominant values share the N - D ranges.
    let limits: Vec<Option<i128>> = std::iter::once(None)
        .chain(dominant.iter().map(|&(value, _)| Some(value)))
        .chain(std::iter::once(None))
        .collect();
    let segments: Vec<Segment> = limits
        .windows(2)
        .map(|pair| Segment::new(&g, pair[0], pair[1]))
        .collect();
    let ranges = n - i128::try_from(dominant.len()).unwrap_or(i128::MAX);
    let sizes: Vec<i128> = segments.iter().map(|segment| segment.rows).collect();
    let counts = allocate(&sizes, ranges);

    let quoted = format!("\"{}\"", input.column().replace('"', "\"\""));
    // The ordinals are all in range, so their text and literals cannot fail.
    let text = |ordinal: i128| codec.text(ordinal).unwrap_or_default();
    let literal = |ordinal: i128| codec.literal(ordinal).unwrap_or_default();
    let mut predicates = vec![Predicate {
        kind: Kind::Null,
        sql: format!("{quoted} IS NULL"),
        estimated_rows: input.null_rows(),
    }];
    for (j, segment) in segments.iter().enumerate() {
        predicates.extend(ranges_of(&g, codec, &quoted, segment, counts[j]));
        if let Some(&(value, rows)) = dominant.get(j) {
            predicates.push(Predicate {
                kind: Kind::Value(text(value)),
                sql: format!("{quoted} = {}", literal(value)),
                estimated_rows: rows,
            });
        }
    }

    let planned = i128::try_from(predicates.len()).unwrap_or(i128::MAX) - 1;
    let warnings = warnings(input, &dominant, &not_isolated, planned, total);
    let largest = predicates
        .iter()
        .map(|predicate| predicate.estimated_rows)
        .max()
        .unwrap_or_default();
    let skew_milli = (total != 0).then(|| (2 * largest * n * 1000 + total) / (2 * total));
    Plan {
        column: input.column().to_owned(),
        type_name: codec.type_name().to_owned(),
        scale: codec.scale(),
        requested_partitions: n,
        null_rows: input.null_rows(),
        total_rows: total,
        parts: predicates,
        skew_milli,
        warnings,
        source: input.source().clone(),
    }
}

/// The range predicates of a segment that has `count` ranges, in value order.
/// The first starts just above the segment's lower limit, and each other at a
/// split point (4.3 and section 5).
fn ranges_of(
    g: &Estimate,
    codec: &Codec,
    quoted: &str,
    segment: &Segment,
    count: i128,
) -> Vec<Predicate> {
    // The ordinals are all in range, so their text and literals cannot fail.
    let text = |ordinal: i128| codec.text(ordinal).unwrap_or_default();
    let literal = |ordinal: i128| codec.literal(ordinal).unwrap_or_default();
    let mut cuts = vec![segment.lower];
    cuts.extend(split_points(g, codec, segment, count).into_iter().map(Some));
    cuts.push(segment.upper);
    let mut predicates = Vec::with_capacity(cuts.len() - 1);
    for (k, pair) in cuts.windows(2).enumerate() {
        let (start, end) = (pair[0], pair[1]);
        let lower = start.map(|start| Bound {
            op: if k == 0 { Op::Above } else { Op::AtLeast },
            value: text(start),
        });
        let upper = end.map(|end| Bound {
            op: Op::Below,
            value: text(end),
        });
        let parts: Vec<String> = [(&lower, start), (&upper, end)]
            .into_iter()
            .filter_map(|(bound, ordinal)| {
                Some(format!(
                    "{quoted} {} {}",
                    bound.as_ref()?.op.as_str(),
                    literal(ordinal?)
                ))
            })
            .collect();
        let sql = if parts.is_empty() {
            format!("{quoted} IS NOT NULL")
        } else {
            parts.join(" AND ")
        };
        let rows = g.at(end, true) - g.at(start, false);
        predicates.push(Predicate {
            kind: Kind::Range { lower, upper },
            sql,
            estimated_rows: rows,
        });
    }
    predicates
}

/// The estimate model `G` of spec section 3, without the dominant values.
///
/// `below(x)` is the number of rows that are not NULL, not a dominant value,
/// and lie below `x`. It takes O(log n) time: prefix sums over the common
/// values and the buckets, and a binary search for `x`.
struct Estimate<'a> {
    values: Vec<i128>,
    value_rows: Vec<i128>,
    bounds: &'a [i128],
    bucket_rows: Vec<i128>,
    top: i128,
}

impl<'a> Estimate<'a> {
    /// The model for an input whose `dominant` values are left out.
    fn new(input: &'a PlanInput, dominant: &[(i128, i128)]) -> Self {
        let dominant: BTreeSet<i128> = dominant.iter().map(|&(value, _)| value).collect();
        let rest = input
            .mcv()
            .iter()
            .filter(|(value, _)| !dominant.contains(value));
        let values: Vec<i128> = rest.clone().map(|&(value, _)| value).collect();
        let value_rows: Vec<i128> = std::iter::once(0)
            .chain(rest.scan(0, |sum, &(_, rows)| {
                *sum += rows;
                Some(*sum)
            }))
            .collect();
        let bounds = input.bounds();
        let buckets = i128::try_from(bounds.len().saturating_sub(1)).unwrap_or(i128::MAX);
        let h = input.histogram_rows();
        // Bucket i (1 to m) holds i*H/m - (i-1)*H/m rows, so the first i hold i*H/m.
        let bucket_rows: Vec<i128> = if buckets == 0 {
            vec![0]
        } else {
            (0..=buckets).map(|i| i * h / buckets).collect()
        };
        let top = value_rows.last().copied().unwrap_or_default()
            + bucket_rows.last().copied().unwrap_or_default();
        Self {
            values,
            value_rows,
            bounds,
            bucket_rows,
            top,
        }
    }

    /// `G(x)`.
    fn below(&self, x: i128) -> i128 {
        let values = self.value_rows[self.values.partition_point(|&value| value < x)];
        values + self.histogram_below(x)
    }

    fn histogram_below(&self, x: i128) -> i128 {
        let (bounds, cumulative) = (self.bounds, &self.bucket_rows);
        // Bounds before index p are below x, so buckets 1 to p - 1 lie wholly
        // below it. Bucket p ends at bounds[p]: whole if that bound is x (as
        // lo < hi = x), otherwise lo < x < hi and the rows below x are
        // interpolated.
        let p = bounds.partition_point(|&bound| bound < x);
        if p == 0 {
            return 0;
        }
        if p == bounds.len() {
            return cumulative[cumulative.len() - 1];
        }
        let rows = cumulative[p] - cumulative[p - 1];
        let (low, high) = (bounds[p - 1], bounds[p]);
        // The product is below 2^63 * 2^64, which fits in an i128 (spec
        // section 2), and both sides are positive, so `/` is floor division.
        let partial = if high == x {
            rows
        } else {
            rows * (x - low) / (high - low)
        };
        cumulative[p - 1] + partial
    }

    /// `G` at a segment limit, where `None` is -∞ or, if `upper`, +∞.
    fn at(&self, limit: Option<i128>, upper: bool) -> i128 {
        match limit {
            None if upper => self.top,
            None => 0,
            Some(limit) => self.below(limit),
        }
    }
}

/// The ordinals between two neighbouring limits, both excluded (4.2).
///
/// `lower` is a dominant value or `None` for -∞; `upper` a dominant value or
/// `None` for +∞. `base` and `top` are `G` at the two limits.
struct Segment {
    lower: Option<i128>,
    upper: Option<i128>,
    base: i128,
    top: i128,
    rows: i128,
}

impl Segment {
    fn new(g: &Estimate, lower: Option<i128>, upper: Option<i128>) -> Self {
        let base = g.at(lower, false);
        let top = g.at(upper, true);
        Self {
            lower,
            upper,
            base,
            top,
            rows: top - base,
        }
    }
}

/// A segment's share in the allocation of 4.2: its rows per range, compared
/// exactly as a fraction, and on a tie the lowest segment first.
#[derive(PartialEq, Eq)]
struct Share {
    rows: i128,
    ranges: i128,
    segment: usize,
}

impl Ord for Share {
    fn cmp(&self, other: &Self) -> Ordering {
        // a / b against c / d is a * d against c * b; rows are below 2^63 and
        // ranges at most 10000, so the products fit.
        (self.rows * other.ranges)
            .cmp(&(other.rows * self.ranges))
            .then_with(|| other.segment.cmp(&self.segment))
    }
}

impl PartialOrd for Share {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Give each segment one range, then each extra range to the largest share (4.2).
fn allocate(sizes: &[i128], ranges: i128) -> Vec<i128> {
    let mut counts = vec![1; sizes.len()];
    let mut heap: BinaryHeap<Share> = sizes
        .iter()
        .enumerate()
        .map(|(segment, &rows)| Share {
            rows,
            ranges: 1,
            segment,
        })
        .collect();
    let extra = ranges - i128::try_from(sizes.len()).unwrap_or(i128::MAX);
    for _ in 0..extra {
        let Some(largest) = heap.pop() else { break };
        let j = largest.segment;
        counts[j] += 1;
        heap.push(Share {
            rows: sizes[j],
            ranges: counts[j],
            segment: j,
        });
    }
    counts
}

/// The split points of a segment that has `count` ranges (4.3).
fn split_points(g: &Estimate, codec: &Codec, segment: &Segment, count: i128) -> Vec<i128> {
    let (lower, base, top) = (segment.lower, segment.base, segment.top);
    let max_ordinal = codec.max_ordinal();
    // G leaves out the dominant value at `lower`, so G(lower) - base is 0, and
    // a search that starts at `lower` or below finds the same x_hi: the
    // mutants of `+ 1` here are equivalent (rust/.cargo/mutants.toml).
    let first = lower.map_or(codec.min_ordinal(), |lower| lower + 1);
    let mut points = Vec::new();
    let mut previous = base;
    for q in 1..count {
        let target = q * segment.rows / count;
        if target == 0 {
            continue;
        }
        // x_hi: the smallest ordinal from `first` with G(x) - G(L) >= target.
        // It is at most MAX + 1, as the segment holds more than `target` rows.
        // Searching only up to MAX gives the same point: then x_hi is MAX, at
        // a negative distance, where the full search picks x_lo = MAX
        // (an equivalent mutant, rust/.cargo/mutants.toml).
        let (mut lo, mut hi) = (first, max_ordinal + 1);
        while lo < hi {
            // Floor division, as in the spec, even when lo + hi is negative.
            let mid = (lo + hi).div_euclid(2);
            if g.below(mid) - base >= target {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        let (x_hi, x_lo) = (lo, lo - 1);
        let mut options = Vec::with_capacity(2);
        if x_hi <= max_ordinal {
            options.push((g.below(x_hi) - base - target, x_hi));
        }
        // With `>=`, x_lo could be `lower` when x_hi is `first`. That point
        // adds nothing, as its G is `base`, and a later target, at least half
        // of G(first) - base, picks `first`: the same points (an equivalent
        // mutant, rust/.cargo/mutants.toml).
        if lower.is_none_or(|lower| x_lo > lower) {
            options.push((target - (g.below(x_lo) - base), x_lo));
        }
        // Nearer to the target wins; on a tie, x_hi, which is listed first.
        // There is always an option: x_lo is one whenever x_hi is beyond MAX.
        // Were there none, x_hi would be MAX + 1, whose G is the segment's
        // top, so the check below would add no point.
        let point = options
            .iter()
            .min_by_key(|&&(distance, _)| distance)
            .map_or(x_hi, |&(_, point)| point);
        let rows = g.below(point);
        if previous < rows && rows < top {
            points.push(point);
            previous = rows;
        }
    }
    points
}

/// The warnings of spec section 8, in its order.
fn warnings(
    input: &PlanInput,
    dominant: &[(i128, i128)],
    not_isolated: &[(i128, i128)],
    planned: i128,
    total: i128,
) -> Vec<PlanWarning> {
    let codec = input.codec();
    let text = |ordinal: i128| codec.text(ordinal).unwrap_or_default();
    let n = input.partitions();
    let mut warnings: Vec<PlanWarning> = dominant
        .iter()
        .map(|&(value, rows)| PlanWarning {
            value: Some(text(value)),
            rows: Some(rows),
            ..PlanWarning::new(
                WarningCode::DominantValue,
                format!(
                    "{} holds {rows} rows, at least one partition's share; it cannot be split further on this column",
                    text(value)
                ),
            )
        })
        .collect();
    warnings.extend(not_isolated.iter().map(|&(value, rows)| PlanWarning {
        value: Some(text(value)),
        rows: Some(rows),
        ..PlanWarning::new(
            WarningCode::DominantValueNotIsolated,
            format!(
                "{} holds {rows} rows, at least one partition's share, but {n} partitions are too few to give it its own predicate",
                text(value)
            ),
        )
    }));
    if planned < n {
        warnings.push(PlanWarning {
            requested: Some(n),
            planned: Some(planned),
            ..PlanWarning::new(
                WarningCode::FewerPartitions,
                format!(
                    "planned {planned} of the {n} requested partitions: the column has too few distinct values, or too many rows at single values"
                ),
            )
        });
    }
    if input.null_rows() * n > total {
        warnings.push(PlanWarning {
            rows: Some(input.null_rows()),
            ..PlanWarning::new(
                WarningCode::NullsExceedShare,
                format!(
                    "{} NULL rows are more than one partition's share",
                    input.null_rows()
                ),
            )
        });
    }
    if total == 0 {
        warnings.push(PlanWarning::new(
            WarningCode::NoRows,
            "the statistics show no rows; the plan still reads rows added later",
        ));
    }
    warnings
}

#[cfg(test)]
mod tests {
    // The private parts of the planner, with the cases of the Python core's
    // tests of Estimate and _allocate.
    use std::cmp::Ordering;

    use super::{Estimate, Share, allocate};
    use crate::inputs::parse_input;
    use crate::json;

    fn input(bounds: Option<&str>, rows: i128, mcv: &str) -> crate::inputs::PlanInput {
        let histogram = bounds.map_or("null".to_owned(), |bounds| {
            format!(r#"{{"bounds": {bounds}, "rows": {rows}}}"#)
        });
        let text = format!(
            r#"{{"column": "c", "type": "int4", "partitions": 4, "null_rows": 0, "mcv": {mcv}, "histogram": {histogram}}}"#
        );
        parse_input(&json::parse(&text).unwrap()).unwrap()
    }

    // Section 3: G, the rows below x.
    #[test]
    fn estimate() {
        // The bounds, the histogram rows, the common values, and G at some points.
        type Case<'a> = (Option<&'a str>, i128, &'a str, &'a [(i128, i128)]);
        let cases: [Case; 5] = [
            (
                Some(r#"["1", "10", "20", "1000000"]"#),
                900,
                "[]",
                &[
                    (8, 233),
                    (7, 200),
                    (1, 0),
                    (0, 0),
                    (10, 300),
                    (10_000_000, 900),
                ],
            ),
            (
                Some(r#"["1", "10", "20", "1000000"]"#),
                1000,
                "[]",
                &[(10, 333), (20, 666), (1_000_000, 1000)],
            ),
            (
                Some(r#"["1", "5", "5", "5", "100"]"#),
                400,
                "[]",
                &[(4, 75), (5, 100), (6, 301), (100, 400)],
            ),
            (Some(r#"["5", "5"]"#), 10, "[]", &[(5, 0), (6, 10)]),
            (
                None,
                0,
                r#"[{"value": "3", "rows": 4}, {"value": "9", "rows": 6}]"#,
                &[(3, 0), (4, 4), (9, 4), (10, 10)],
            ),
        ];
        for (bounds, rows, mcv, points) in cases {
            let input = input(bounds, rows, mcv);
            let g = Estimate::new(&input, &[]);
            for &(x, below) in points {
                assert_eq!(g.below(x), below, "G({x})");
            }
            let all: i128 = input.mcv().iter().map(|&(_, rows)| rows).sum::<i128>() + rows;
            assert_eq!((g.at(None, false), g.at(None, true)), (0, all));
        }
    }

    #[test]
    fn estimate_leaves_out_dominant_values() {
        let input = input(
            None,
            0,
            r#"[{"value": "3", "rows": 4}, {"value": "9", "rows": 6}]"#,
        );
        let g = Estimate::new(&input, &[(3, 4)]);
        assert_eq!((g.below(10), g.at(None, true)), (6, 6));
    }

    // Section 4.2: sharing the ranges among segments.
    #[test]
    fn allocate_gives_each_extra_range_to_the_largest_share() {
        let cases: [(&[i128], i128, &[i128]); 6] = [
            (&[0, 88, 212], 4, &[1, 1, 2]),
            (&[0, 0], 3, &[2, 1]),
            (&[100, 60], 4, &[2, 2]),
            (&[100, 60], 5, &[3, 2]),
            (&[7], 1, &[1]),
            // After 100 gets a second range, both shares are 50: the lower segment wins.
            (&[50, 100], 4, &[2, 2]),
        ];
        for (sizes, ranges, counts) in cases {
            assert_eq!(allocate(sizes, ranges), counts, "{sizes:?} {ranges}");
        }
    }

    #[test]
    fn a_share_is_its_rows_per_range_compared_exactly() {
        let share = |rows, ranges, segment| Share {
            rows,
            ranges,
            segment,
        };
        for ((a, b), order) in [
            // 60 / 1 against 100 / 2, both ways.
            ((share(60, 1, 0), share(100, 2, 1)), Ordering::Greater),
            ((share(100, 2, 1), share(60, 1, 0)), Ordering::Less),
            // 1 / 3 is above 333333333 / 1000000000.
            (
                (share(1, 3, 0), share(333_333_333, 1_000_000_000, 1)),
                Ordering::Greater,
            ),
            // On a tie, the lower segment comes first, as the largest.
            ((share(50, 1, 0), share(100, 2, 1)), Ordering::Greater),
            ((share(100, 2, 1), share(50, 1, 0)), Ordering::Less),
        ] {
            assert_eq!(
                a.cmp(&b),
                order,
                "{} / {} against {} / {}",
                a.rows,
                a.ranges,
                b.rows,
                b.ranges
            );
        }
    }
}
