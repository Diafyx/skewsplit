//! Invariants of every plan (spec sections 4 to 8) on random valid inputs, as
//! `python/tests/property/test_planner_invariants.py` checks them with
//! Hypothesis, of reading array text (spec section 12), as
//! `python/tests/property/test_pgarray_properties.py` does, and of hash
//! predicates (section 16), as `python/tests/property/test_hash_properties.py`
//! does, and of converting a sample (section 14), as
//! `python/tests/property/test_sample_properties.py` does.
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

use std::collections::{BTreeMap, BTreeSet};

use proptest::collection::vec;
use proptest::prelude::*;
use proptest::sample::{Index, select};
use proptest::test_runner::FileFailurePersistence;
use skewsplit::codecs::{Codec, codec_for};
use skewsplit::error::ErrorCode;
use skewsplit::hashing::hash_plan;
use skewsplit::json::{Object, Value};
use skewsplit::pgarray::{parse_array, parse_values};
use skewsplit::plan::{Kind, Op, Plan, Predicate, WarningCode};
use skewsplit::planner::plan;
use skewsplit::sample::from_sample;

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

/// A total of rows up to 2^63 - 1, and the NULL rows among them: the second
/// range depends on the first draw, which `prop_flat_map` allows.
fn hash_rows() -> impl Strategy<Value = (i128, i128)> {
    (0..=i128::from(i64::MAX)).prop_flat_map(|rows| (Just(rows), 0..=rows))
}

/// A hash plan of `rows` rows, `null_rows` of them NULL, in `n` partitions.
fn hash_input(n: i128, null_rows: i128, rows: i128) -> Plan {
    hash_plan(&object([
        ("column", Value::from("c")),
        ("type", Value::from("uuid")),
        ("partitions", Value::from(n)),
        ("null_rows", Value::from(null_rows)),
        ("non_null_rows", Value::from(rows - null_rows)),
    ]))
    .unwrap()
}

proptest! {
    #![proptest_config(config())]

    // PostgreSQL's `%` truncates toward zero, as Rust's does on integers, so
    // the remainder of a negative hash is negative. The predicates' expression
    // `((h % N) + N) % N` must still give every hash exactly one remainder.
    #[test]
    fn every_hash_has_exactly_one_remainder(h in any::<i64>(), n in 1..=10_000_i64) {
        let inner = h % n;
        prop_assert!(-n < inner && inner < n);
        // No step leaves bigint, so the expression never overflows.
        let remainder = inner.checked_add(n).unwrap() % n;
        prop_assert!((0..n).contains(&remainder));
        prop_assert_eq!(remainder, h.rem_euclid(n));
        let result = hash_input(i128::from(n), 0, 1);
        let suffix = format!(" = {remainder}");
        let matching: Vec<&Kind> = result.parts[1..]
            .iter()
            .filter(|part| part.sql.ends_with(&suffix))
            .map(|part| &part.kind)
            .collect();
        prop_assert_eq!(matching, [&Kind::Hash(i128::from(remainder))]);
    }

    #[test]
    fn the_estimates_add_up_and_differ_by_at_most_one(
        n in 1..=10_000_i128,
        (rows, null_rows) in hash_rows(),
    ) {
        let result = hash_input(n, null_rows, rows);
        let estimates: Vec<i128> = result.parts[1..].iter().map(|part| part.estimated_rows).collect();
        prop_assert_eq!(i128::try_from(estimates.len()).unwrap(), n);
        prop_assert_eq!(result.planned_partitions(), n);
        prop_assert_eq!(estimates.iter().sum::<i128>(), rows - null_rows);
        let (low, high) = (estimates.iter().min().unwrap(), estimates.iter().max().unwrap());
        prop_assert!(high - low <= 1, "{} and {}", low, high);
        let remainders: Vec<&Kind> = result.parts[1..].iter().map(|part| &part.kind).collect();
        let expected: Vec<Kind> = (0..n).map(Kind::Hash).collect();
        prop_assert_eq!(remainders, expected.iter().collect::<Vec<_>>());
        prop_assert_eq!(result.warnings[0].code, WarningCode::FullScans);
    }
}

/// The special values of `numeric`.
const NUMERIC_SPECIAL: [&str; 3] = ["NaN", "Infinity", "-Infinity"];

/// A sample as the Python test draws it: the ordinary values' counts, the
/// sampled NULL rows, the special values' counts, `T` (`None` for the sample
/// size), and the entries in a random order.
#[derive(Clone, Debug)]
struct Sampled {
    counts: BTreeMap<i128, i128>,
    nulls: i128,
    specials: BTreeMap<&'static str, i128>,
    total: Option<i128>,
    entries: Vec<(String, i128)>,
}

fn samples() -> impl Strategy<Value = Sampled> {
    // A few heavy values among many light ones, so every rule of section 14 is reached.
    let light = proptest::collection::btree_map(-1_000_000..=1_000_000_i128, 1..=3_i128, 0..=1500);
    let heavy = proptest::collection::btree_map(-50..=50_i128, 1..=5000_i128, 0..=6);
    let specials =
        proptest::collection::btree_map(select(&NUMERIC_SPECIAL[..]), 1..=100_i128, 0..=3);
    let total = prop_oneof![Just(None), (0..=1_000_000_000_000_i128).prop_map(Some)];
    (light, heavy, specials, 0..=5000_i128, total).prop_flat_map(
        |(light, heavy, specials, nulls, total)| {
            let mut counts = light;
            counts.extend(heavy);
            let mut pairs: Vec<(String, i128)> = counts
                .iter()
                .map(|(value, rows)| (value.to_string(), *rows))
                .collect();
            pairs.extend(
                specials
                    .iter()
                    .map(|(value, rows)| ((*value).to_owned(), *rows)),
            );
            let fixed = (counts, nulls, specials, total);
            (Just(fixed), Just(pairs).prop_shuffle()).prop_map(
                |((counts, nulls, specials, total), entries)| Sampled {
                    counts,
                    nulls,
                    specials,
                    total,
                    entries,
                },
            )
        },
    )
}

fn sample_entry(value: &str, rows: i128) -> Value {
    object([("value", Value::from(value)), ("rows", Value::from(rows))])
}

/// `count × total / size` rounded half up, from the quotient and the
/// remainder, so it shares no code with the conversion's formula.
fn scaled(count: i128, total: i128, size: i128) -> i128 {
    if size == 0 {
        return 0;
    }
    let (quotient, remainder) = (count * total / size, count * total % size);
    quotient + i128::from(2 * remainder >= size)
}

/// Section 14 step by step, writing out the list of values one by one: the
/// NULL rows, `mcv`, the histogram and the unestimated rows.
fn sample_reference(drawn: &Sampled, total: i128) -> (i128, Value, Value, i128) {
    let ordinary: i128 = drawn.counts.values().sum();
    let size = drawn.nulls + ordinary + drawn.specials.values().sum::<i128>();
    let is_common = |rows: i128| rows >= 2 && rows * 1000 >= ordinary;
    let listed: Vec<i128> = drawn
        .counts
        .iter()
        .filter(|&(_, &rows)| !is_common(rows))
        .flat_map(|(&value, &rows)| std::iter::repeat_n(value, usize::try_from(rows).unwrap()))
        .collect();
    let null_rows = scaled(drawn.nulls, total, size);
    let unestimated = scaled(drawn.specials.values().sum(), total, size);
    let mcv: Vec<(i128, i128)> = drawn
        .counts
        .iter()
        .filter(|&(_, &rows)| is_common(rows))
        .map(|(&value, &rows)| (value, scaled(rows, total, size).max(1)))
        .collect();
    let histogram = if listed.is_empty() {
        Value::Null
    } else {
        let h = i128::try_from(listed.len()).unwrap();
        let m = (h - 1).clamp(1, 1000);
        let rows =
            (total - null_rows - mcv.iter().map(|&(_, rows)| rows).sum::<i128>() - unestimated)
                .max(0);
        let bounds = (0..=m)
            .map(|j| Value::from(listed[usize::try_from(j * (h - 1) / m).unwrap()].to_string()))
            .collect();
        object([
            ("bounds", Value::Array(bounds)),
            ("rows", Value::from(rows)),
        ])
    };
    let mcv = mcv
        .into_iter()
        .map(|(value, rows)| sample_entry(&value.to_string(), rows))
        .collect();
    (null_rows, Value::Array(mcv), histogram, unestimated)
}

fn sample_input(
    type_name: &str,
    partitions: i128,
    total: i128,
    nulls: i128,
    entries: Vec<Value>,
) -> Value {
    let mut input = Object::new();
    input.insert("column", Value::from("c"));
    input.insert("type", Value::from(type_name));
    if type_name == "numeric" {
        input.insert("scale", Value::from(0));
    }
    input.insert("partitions", Value::from(partitions));
    input.insert("rows_estimate", Value::from(total));
    input.insert("sample_null_rows", Value::from(nulls));
    input.insert("sample_values", Value::Array(entries));
    Value::Object(input)
}

/// Rows of a column, each NULL or an int8 value from a narrow or a wide
/// range, with lengths spread evenly on a log scale up to 4000, so most lists
/// are short and every case stays fast.
fn column_rows() -> impl Strategy<Value = Vec<Option<i128>>> {
    let row = prop_oneof![
        Just(None),
        (-30..=30_i128).prop_map(Some),
        (-10_000..=10_000_i128).prop_map(Some),
    ];
    (0..=12_u32).prop_flat_map(move |bits| vec(row.clone(), 0..=(1_usize << bits).min(4000)))
}

proptest! {
    #![proptest_config(config())]

    #[test]
    fn the_conversion_follows_section_14_step_by_step(drawn in samples()) {
        let size = drawn.nulls + drawn.entries.iter().map(|(_, rows)| rows).sum::<i128>();
        let total = drawn.total.unwrap_or(size);
        let entries = drawn.entries.iter().map(|(value, rows)| sample_entry(value, *rows)).collect();
        let result = from_sample(&sample_input("numeric", 4, total, drawn.nulls, entries)).unwrap();
        let (null_rows, mcv, histogram, unestimated) = sample_reference(&drawn, total);
        prop_assert_eq!(result.get("null_rows"), Some(&Value::from(null_rows)));
        prop_assert_eq!(result.get("mcv"), Some(&mcv));
        prop_assert_eq!(result.get("histogram"), Some(&histogram));
        let source = result.get("source").unwrap();
        prop_assert_eq!(source.get("unestimated_rows"), Some(&Value::from(unestimated)));
        prop_assert_eq!(source.get("sample_rows"), Some(&Value::from(size)));
        prop_assert!(mcv.as_array().unwrap().len() <= 1000);
        if let Some(bounds) = histogram.get("bounds").and_then(Value::as_array) {
            prop_assert!(bounds.len() <= 1001);
        }
    }

    // Spec section 14: when every row was read, the NULL and value predicates
    // are exact and each range reads within 2 x B rows of its estimate.
    #[test]
    fn a_plan_from_every_row_is_within_two_buckets_of_the_rows(
        rows in column_rows(),
        partitions in 1..=64_i128,
    ) {
        let mut counts: BTreeMap<i128, i128> = BTreeMap::new();
        for value in rows.iter().flatten() {
            *counts.entry(*value).or_default() += 1;
        }
        let nulls = i128::try_from(rows.iter().filter(|row| row.is_none()).count()).unwrap();
        let total = i128::try_from(rows.len()).unwrap();
        let entries = counts.iter().map(|(value, rows)| sample_entry(&value.to_string(), *rows)).collect();
        let converted = from_sample(&sample_input("int8", partitions, total, nulls, entries)).unwrap();
        let bucket = match converted.get("histogram").and_then(Value::as_object) {
            Some(histogram) => {
                let rows = histogram.get("rows").and_then(Value::as_i128).unwrap();
                let buckets = i128::try_from(histogram.get("bounds").and_then(Value::as_array).unwrap().len()).unwrap() - 1;
                (rows + buckets - 1) / buckets
            }
            None => 0,
        };
        let result = plan(&converted).unwrap();
        prop_assert_eq!(result.total_rows, total);
        let codec = codec_for("int8", None).unwrap();
        // Each distinct value matches exactly one predicate, which the range
        // planner's own properties check, so its rows go to the first that matches.
        let mut actual = vec![0; result.parts.len()];
        actual[0] = nulls;
        for (&value, &rows) in &counts {
            let at = result.parts.iter().position(|part| matches(&codec, part, value)).unwrap();
            actual[at] += rows;
        }
        for (part, actual) in result.parts.iter().zip(actual) {
            if matches!(part.kind, Kind::Range { .. }) {
                prop_assert!((actual - part.estimated_rows).abs() <= 2 * bucket, "{}", part.sql);
            } else {
                prop_assert_eq!(actual, part.estimated_rows, "{}", part.sql);
            }
        }
    }
}
