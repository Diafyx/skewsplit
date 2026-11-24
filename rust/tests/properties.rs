//! Invariants of every plan (spec sections 4 to 8) on random valid inputs, as
//! `python/tests/property/test_planner_invariants.py` checks them with
//! Hypothesis, and of reading array text (spec section 12), as
//! `python/tests/property/test_pgarray_properties.py` does.
//!
//! `PROPTEST_PROFILE` sets the number of cases of each test, as
//! `HYPOTHESIS_PROFILE` sets the examples for Python: `dev` (the default) 100,
//! `ci` 1000, `nightly` 20000 and `fuzz` 100000. A failing case is shrunk and
//! printed, and proptest saves its seed in `tests/properties.proptest-regressions`.
//! Commit that file: proptest replays the seeds in it first, on every run.
//!
//! The Python test that runs the predicates' SQL in `SQLite` has no
//! counterpart here, since the standard library has no SQL engine. The SQL
//! text is the same in both implementations, as the golden fixtures check.

use std::collections::BTreeSet;

use proptest::collection::vec;
use proptest::prelude::*;
use proptest::sample::{Index, select};
use proptest::test_runner::FileFailurePersistence;
use skewsplit::codecs::{Codec, codec_for};
use skewsplit::error::ErrorCode;
use skewsplit::json::{Object, Value};
use skewsplit::pgarray::{parse_array, parse_values};
use skewsplit::plan::{Kind, Op, Plan, Predicate, WarningCode};
use skewsplit::planner::plan;

const TYPES: [&str; 7] = [
    "int2",
    "int4",
    "int8",
    "numeric",
    "date",
    "timestamp",
    "timestamptz",
];
/// Names that need quoting, including a double quote and an SQL keyword.
const COLUMNS: [&str; 5] = ["c", "customer_id", "a\"b", "Order Id", "select"];
/// The widths of the window of the type that the values are drawn from.
const WIDTHS: [i128; 4] = [10, 1000, 1_000_000_000, 1 << 70];
/// The most values an input has: 12 drawn, and the two limits of the type.
const POOL: usize = 14;

fn config() -> ProptestConfig {
    let profile = std::env::var("PROPTEST_PROFILE").unwrap_or_default();
    let cases = match profile.as_str() {
        "" | "dev" => 100,
        "ci" => 1000,
        "nightly" => 20_000,
        "fuzz" => 100_000,
        other => panic!("PROPTEST_PROFILE is dev, ci, nightly or fuzz, not {other:?}"),
    };
    ProptestConfig {
        cases,
        // tests/properties.proptest-regressions, next to this file.
        failure_persistence: Some(Box::new(FileFailurePersistence::WithSource(
            "proptest-regressions",
        ))),
        ..ProptestConfig::default()
    }
}

/// The random choices an input is built from. The values are drawn as offsets
/// and indexes, so that they fit whatever type is drawn.
#[derive(Clone, Debug)]
struct Draw {
    type_name: &'static str,
    scale: i64,
    width: i128,
    start: u128,
    /// Start at the lowest (`false`) or the highest (`true`) window of the type.
    edge: Option<bool>,
    offsets: Vec<u128>,
    /// Add the type's limits, where `x_hi` can be MAX + 1.
    limits: bool,
    bounds: Vec<Index>,
    no_histogram: bool,
    /// For each value in the pool that is not a bound, its rows if it is a
    /// common value.
    mcv: Vec<Option<i128>>,
    column: &'static str,
    partitions: i128,
    null_rows: i128,
    histogram_rows: i128,
}

fn draws() -> impl Strategy<Value = Draw> {
    // Some values far heavier than the rest, so that many become dominant, and
    // a few row counts so small that dominant values often tie.
    let rows = prop_oneof![
        1..=3_i128,
        1..=100_i128,
        1..=1_000_000_000_000_i128,
        10_000_000_000_000..=1_000_000_000_000_000_i128,
    ];
    (
        (
            select(TYPES.to_vec()),
            0..=3_i64,
            select(WIDTHS.to_vec()),
            any::<u128>(),
            proptest::option::of(any::<bool>()),
        ),
        (
            vec(any::<u128>(), 0..=12),
            any::<bool>(),
            vec(any::<Index>(), 2..=10),
            any::<bool>(),
            vec(proptest::option::of(rows), POOL),
        ),
        (
            select(COLUMNS.to_vec()),
            prop_oneof![1..=12_i128, 1..=200_i128],
            prop_oneof![Just(0_i128), 0..=1_000_000_i128],
            prop_oneof![0..=100_i128, 0..=1_000_000_000_000_i128],
        ),
    )
        .prop_map(
            |(
                (type_name, scale, width, start, edge),
                (offsets, limits, bounds, no_histogram, mcv),
                (column, partitions, null_rows, histogram_rows),
            )| Draw {
                type_name,
                scale,
                width,
                start,
                edge,
                offsets,
                limits,
                bounds,
                no_histogram,
                mcv,
                column,
                partitions,
                null_rows,
                histogram_rows,
            },
        )
}

/// A valid input, built from a draw: values in a window of the type, which
/// may reach its limits.
struct Case {
    codec: Codec,
    /// The ordinals of the input's common values and histogram bounds.
    ordinals: Vec<i128>,
    mcv: Vec<Value>,
    input: Value,
}

fn object<const N: usize>(entries: [(&str, Value); N]) -> Value {
    let mut object = Object::new();
    for (key, value) in entries {
        object.insert(key, value);
    }
    Value::Object(object)
}

fn ordinal(value: u128) -> i128 {
    i128::try_from(value).unwrap()
}

impl Case {
    fn new(draw: &Draw) -> Self {
        let scale = (draw.type_name == "numeric").then_some(draw.scale);
        let codec = codec_for(draw.type_name, scale).unwrap();
        let (min, max) = (codec.min_ordinal(), codec.max_ordinal());
        let width = draw.width.min(max - min);
        // Every span here is below 2^65, so it fits in a u128.
        let starts = u128::try_from(max - width - min).unwrap() + 1;
        let start = match draw.edge {
            Some(false) => min,
            Some(true) => max - width,
            None => min + ordinal(draw.start % starts),
        };
        let offsets = u128::try_from(width).unwrap() + 1;
        let mut pool: BTreeSet<i128> = draw
            .offsets
            .iter()
            .map(|offset| start + ordinal(offset % offsets))
            .collect();
        if draw.limits {
            pool.extend([min, max]);
        }
        let pool: Vec<i128> = pool.into_iter().collect();
        let mut bounds: Vec<i128> = if pool.is_empty() {
            Vec::new()
        } else {
            draw.bounds
                .iter()
                .map(|index| pool[index.index(pool.len())])
                .collect()
        };
        bounds.sort_unstable();
        if bounds.len() < 2 || draw.no_histogram {
            bounds.clear();
        }
        let text = |ordinal: i128| Value::from(codec.text(ordinal).unwrap());
        let values: Vec<(i128, i128)> = pool
            .iter()
            .zip(&draw.mcv)
            .filter(|(value, _)| !bounds.contains(value))
            .filter_map(|(&value, rows)| rows.map(|rows| (value, rows)))
            .collect();
        let mcv: Vec<Value> = values
            .iter()
            .map(|&(value, rows)| object([("value", text(value)), ("rows", Value::from(rows))]))
            .collect();
        let histogram = if bounds.is_empty() {
            Value::Null
        } else {
            object([
                (
                    "bounds",
                    Value::Array(bounds.iter().map(|&bound| text(bound)).collect()),
                ),
                ("rows", Value::from(draw.histogram_rows)),
            ])
        };
        let Value::Object(mut input) = object([
            ("column", Value::from(draw.column)),
            ("type", Value::from(draw.type_name)),
        ]) else {
            unreachable!("object makes an object");
        };
        if let Some(scale) = scale {
            input.insert("scale", Value::from(i128::from(scale)));
        }
        input.insert("partitions", Value::from(draw.partitions));
        input.insert("null_rows", Value::from(draw.null_rows));
        input.insert("mcv", Value::Array(mcv.clone()));
        input.insert("histogram", histogram);
        let ordinals = values
            .iter()
            .map(|&(value, _)| value)
            .chain(bounds)
            .collect();
        Case {
            codec,
            ordinals,
            mcv,
            input: Value::Object(input),
        }
    }

    /// The input with its common values in another order.
    fn with_mcv(&self, mcv: Vec<Value>) -> Value {
        let mut input = self.input.clone();
        let Value::Object(object) = &mut input else {
            unreachable!("an input is an object");
        };
        object.insert("mcv", Value::Array(mcv));
        input
    }

    fn plan(&self) -> Plan {
        plan(&self.input).unwrap()
    }
}

/// Whether a non-NULL value matches a predicate, read from its structure.
fn matches(codec: &Codec, predicate: &Predicate, ordinal: i128) -> bool {
    let parse = |text: &str| codec.parse(text).unwrap();
    match &predicate.kind {
        Kind::Null => false,
        Kind::Value(value) => ordinal == parse(value),
        Kind::Range { lower, upper } => {
            let above_lower = lower.as_ref().is_none_or(|bound| match bound.op {
                Op::AtLeast => ordinal >= parse(&bound.value),
                Op::Above => ordinal > parse(&bound.value),
                Op::Below => panic!("a lower bound is >= or >: {}", predicate.sql),
            });
            let below_upper = upper.as_ref().is_none_or(|bound| {
                assert_eq!(
                    bound.op,
                    Op::Below,
                    "an upper bound is <: {}",
                    predicate.sql
                );
                ordinal < parse(&bound.value)
            });
            above_lower && below_upper
        }
        Kind::Hash(_) => panic!("the range planner makes no hash predicates"),
    }
}

/// The type limits, every input value and boundary with its neighbours, and
/// more.
fn probes(case: &Case, result: &Plan, extra: &[i64]) -> Vec<i128> {
    let codec = &case.codec;
    let (min, max) = (codec.min_ordinal(), codec.max_ordinal());
    let mut ordinals = case.ordinals.clone();
    for predicate in &result.parts {
        if let Kind::Range { lower, upper } = &predicate.kind {
            for bound in [lower, upper].into_iter().flatten() {
                ordinals.push(codec.parse(&bound.value).unwrap());
            }
        }
    }
    let mut points: BTreeSet<i128> = [min, max]
        .into_iter()
        .chain(extra.iter().map(|&x| i128::from(x)))
        .collect();
    for ordinal in ordinals {
        points.extend([ordinal - 1, ordinal, ordinal + 1]);
    }
    points
        .into_iter()
        .filter(|point| (min..=max).contains(point))
        .collect()
}

proptest! {
    #![proptest_config(config())]

    #[test]
    fn every_value_matches_exactly_one_predicate(draw in draws(), extra in vec(any::<i64>(), 0..=5)) {
        let case = Case::new(&draw);
        let result = case.plan();
        let nulls = result.parts.iter().filter(|part| part.kind == Kind::Null).count();
        prop_assert_eq!(nulls, 1);
        prop_assert_eq!(&result.parts[0].kind, &Kind::Null);
        for ordinal in probes(&case, &result, &extra) {
            let hits: Vec<&str> = result
                .parts
                .iter()
                .filter(|part| matches(&case.codec, part, ordinal))
                .map(|part| part.sql.as_str())
                .collect();
            prop_assert_eq!(hits.len(), 1, "{} matches {:?}", case.codec.text(ordinal).unwrap(), hits);
        }
    }

    #[test]
    fn the_plan_is_consistent(draw in draws()) {
        let result = Case::new(&draw).plan();
        let n = draw.partitions;
        let total = result.total_rows;
        let codes: Vec<WarningCode> = result.warnings.iter().map(|warning| warning.code).collect();
        let estimates: Vec<i128> = result.parts.iter().map(|part| part.estimated_rows).collect();
        // Partitions: never more than N, and fewer only with a warning that says so.
        let planned = result.planned_partitions();
        prop_assert!((1..=n).contains(&planned), "{} of {} partitions", planned, n);
        prop_assert_eq!(codes.contains(&WarningCode::FewerPartitions), planned < n);
        // Rows: the estimates add up, and every dominant value has its own predicate.
        prop_assert!(estimates.iter().all(|&rows| rows >= 0), "{:?}", estimates);
        prop_assert_eq!(estimates.iter().sum::<i128>(), total);
        let values: Vec<&str> = result
            .parts
            .iter()
            .filter_map(|part| match &part.kind {
                Kind::Value(value) => Some(value.as_str()),
                _ => None,
            })
            .collect();
        let dominant: Vec<&str> = result
            .warnings
            .iter()
            .filter(|warning| warning.code == WarningCode::DominantValue)
            .filter_map(|warning| warning.value.as_deref())
            .collect();
        prop_assert_eq!(&values, &dominant);
        prop_assert!(i128::try_from(values.len()).unwrap() <= (n - 1) / 2);
        // 4.3: in a segment with more than one range, every range holds a row.
        let mut segments: Vec<Vec<i128>> = vec![Vec::new()];
        for part in &result.parts[1..] {
            if matches!(part.kind, Kind::Value(_)) {
                segments.push(Vec::new());
            } else {
                segments.last_mut().unwrap().push(part.estimated_rows);
            }
        }
        prop_assert!(
            segments.iter().filter(|rows| rows.len() > 1).all(|rows| rows.iter().all(|&r| r >= 1)),
            "{:?}",
            segments
        );
        // The skew score follows from the largest predicate.
        if total > 0 {
            let largest = estimates.iter().max().unwrap();
            prop_assert_eq!(result.skew_milli, Some((2 * largest * n * 1000 + total) / (2 * total)));
        } else {
            prop_assert_eq!(result.skew_milli, None);
            prop_assert_eq!(codes.last(), Some(&WarningCode::NoRows));
        }
    }

    #[test]
    fn the_order_of_common_values_does_not_matter(draw in draws(), swaps in vec(any::<Index>(), POOL)) {
        let case = Case::new(&draw);
        let mut mcv = case.mcv.clone();
        // A Fisher-Yates shuffle.
        for i in (1..mcv.len()).rev() {
            mcv.swap(i, swaps[i].index(i + 1));
        }
        let shuffled = plan(&case.with_mcv(mcv)).unwrap();
        prop_assert_eq!(shuffled.to_value(), case.plan().to_value());
    }
}

// Array text (spec section 12).

/// The characters PostgreSQL quotes an element for.
const SPECIAL: [char; 11] = [
    '{', '}', ',', '"', '\\', ' ', '\t', '\n', '\r', '\x0b', '\x0c',
];
const ARRAY_TYPES: [(&str, Option<i64>); 8] = [
    ("int2", None),
    ("int4", None),
    ("int8", None),
    ("numeric", Some(2)),
    ("numeric", Some(0)),
    ("date", None),
    ("timestamp", None),
    ("timestamptz", None),
];
const MAX_ITEMS: usize = 20;
/// The characters that matter, so that random text often gets past the first
/// check.
const NEAR_ARRAYS: [char; 21] = [
    '{', '}', '[', ']', ':', '=', ',', '"', '\\', ' ', '-', '0', '1', 'N', 'U', 'L', 'n', 'u', 'l',
    '\t', 'x',
];

/// How PostgreSQL prints an element (`array_out` in
/// `src/backend/utils/adt/arrayfuncs.c`): quoted if it is empty, is NULL in
/// any ASCII case, or holds a special character, with `"` and `\` escaped.
fn quote(item: Option<&str>) -> String {
    let Some(item) = item else {
        return "NULL".to_owned();
    };
    if item.is_empty() || item.eq_ignore_ascii_case("NULL") || item.contains(SPECIAL) {
        format!("\"{}\"", item.replace('\\', "\\\\").replace('"', "\\\""))
    } else {
        item.to_owned()
    }
}

/// How PostgreSQL prints an array, with the `[L:U]=` prefix when the lower
/// bound is not 1.
fn array_out(items: &[Option<String>], lower: i64) -> String {
    let prefix = if lower != 1 && !items.is_empty() {
        let count = i64::try_from(items.len()).unwrap();
        format!("[{lower}:{}]=", lower + count - 1)
    } else {
        String::new()
    };
    let items: Vec<String> = items.iter().map(|item| quote(item.as_deref())).collect();
    format!("{prefix}{{{}}}", items.join(","))
}

fn texts() -> impl Strategy<Value = String> {
    prop_oneof![
        any::<String>(),
        vec(select(NEAR_ARRAYS.to_vec()), 0..=30).prop_map(String::from_iter),
    ]
}

/// Lower bounds that leave room for `MAX_ITEMS` elements below the int4
/// maximum.
fn lowers() -> impl Strategy<Value = i64> {
    i64::from(i32::MIN)..=i64::from(i32::MAX) - 20
}

fn array_codec(index: Index) -> Codec {
    let (type_name, scale) = ARRAY_TYPES[index.index(ARRAY_TYPES.len())];
    codec_for(type_name, scale).unwrap()
}

proptest! {
    #![proptest_config(config())]

    #[test]
    fn printed_arrays_read_back(
        items in vec(proptest::option::of(any::<String>()), 0..=MAX_ITEMS),
        lower in lowers(),
    ) {
        prop_assert_eq!(parse_array(&array_out(&items, lower)).unwrap(), items);
    }

    #[test]
    fn any_text_reads_or_is_invalid_array(text in texts()) {
        if let Err(error) = parse_array(&text) {
            prop_assert_eq!(error.code(), ErrorCode::InvalidArray);
        }
    }

    #[test]
    fn printed_values_read_back_in_output_form(
        index in any::<Index>(),
        fractions in vec(any::<u128>(), 0..=MAX_ITEMS),
        lower in lowers(),
    ) {
        let codec = array_codec(index);
        let (min, max) = (codec.min_ordinal(), codec.max_ordinal());
        // Every type's range has fewer than 2^65 ordinals, so it fits a u128.
        let span = u128::try_from(max - min).unwrap() + 1;
        let texts: Vec<String> = fractions
            .iter()
            .map(|fraction| codec.text(min + i128::try_from(fraction % span).unwrap()).unwrap())
            .collect();
        let items: Vec<Option<String>> = texts.iter().cloned().map(Some).collect();
        prop_assert_eq!(parse_values(&array_out(&items, lower), &codec).unwrap(), texts);
    }

    #[test]
    fn any_text_gives_values_or_a_spec_error(index in any::<Index>(), text in texts()) {
        let codec = array_codec(index);
        match parse_values(&text, &codec) {
            Ok(values) => {
                for value in values {
                    let again = codec.text(codec.parse(&value).unwrap()).unwrap();
                    prop_assert_eq!(again, value);
                }
            }
            Err(error) => prop_assert!(
                matches!(
                    error.code(),
                    ErrorCode::InvalidArray | ErrorCode::InvalidValue | ErrorCode::ValueOutOfRange
                ),
                "{:?}",
                error
            ),
        }
    }
}
