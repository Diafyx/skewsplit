//! Converting a sample into a planner input (spec section 14), with the
//! cases of the Python package's `python/tests/unit/test_sample.py`: each rule
//! at its edges. `tests/fixtures.rs` runs every fixture in
//! `spec/fixtures/sample/`.

use skewsplit::ErrorCode;
use skewsplit::error::SpecError;
use skewsplit::json::{self, Object, Value};
use skewsplit::sample::{RESOLUTION, from_sample};

const INT64_MAX: i128 = i64::MAX as i128;
const ENTRIES: &str =
    "sample_values must be a list of objects with a string value and an integer rows";

/// Sampled values and their rows.
type Pairs<'a> = &'a [(&'a str, i128)];

/// A conversion input with these sampled values, and each field set to the
/// JSON text given. `rows_estimate` is the sample size unless given.
fn sample(pairs: &[(&str, i128)], fields: &[(&str, &str)]) -> Value {
    let mut data = Object::new();
    for (key, value) in [
        ("column", Value::from("c")),
        ("type", Value::from("int4")),
        ("partitions", Value::from(4)),
        ("rows_estimate", Value::Null),
        ("sample_null_rows", Value::from(0)),
    ] {
        data.insert(key, value);
    }
    let entries = pairs
        .iter()
        .map(|&(value, rows)| entry(Value::from(value), Value::from(rows)))
        .collect();
    data.insert("sample_values", Value::Array(entries));
    for &(key, text) in fields {
        data.insert(key, json::parse(text).unwrap());
    }
    if data.get("rows_estimate") == Some(&Value::Null) {
        let nulls = data
            .get("sample_null_rows")
            .and_then(Value::as_i128)
            .unwrap();
        let size = nulls + pairs.iter().map(|&(_, rows)| rows).sum::<i128>();
        data.insert("rows_estimate", Value::from(size));
    }
    Value::Object(data)
}

fn entry(value: Value, rows: Value) -> Value {
    let mut entry = Object::new();
    entry.insert("value", value);
    entry.insert("rows", rows);
    Value::Object(entry)
}

/// `count` values from `start`, each sampled once.
fn singles(start: i128, count: i128) -> Vec<(String, i128)> {
    (start..start + count)
        .map(|value| (value.to_string(), 1))
        .collect()
}

/// Pairs with owned texts as pairs for [`sample`].
fn pairs(owned: &[(String, i128)]) -> Vec<(&str, i128)> {
    owned
        .iter()
        .map(|(value, rows)| (value.as_str(), *rows))
        .collect()
}

fn converted(data: &Value) -> Value {
    from_sample(data).expect("a valid sample")
}

fn field<'a>(result: &'a Value, key: &str) -> &'a Value {
    result.get(key).unwrap()
}

fn common_values(data: &Value) -> Vec<String> {
    let result = converted(data);
    field(&result, "mcv")
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| {
            entry
                .get("value")
                .and_then(Value::as_str)
                .unwrap()
                .to_owned()
        })
        .collect()
}

fn histogram(data: &Value) -> Value {
    field(&converted(data), "histogram").clone()
}

fn bounds_of(histogram: &Value) -> Vec<String> {
    histogram
        .get("bounds")
        .and_then(Value::as_array)
        .unwrap()
        .iter()
        .map(|bound| bound.as_str().unwrap().to_owned())
        .collect()
}

fn parsed(text: &str) -> Value {
    json::parse(text).unwrap()
}

fn error(data: &Value) -> SpecError {
    from_sample(data).expect_err("an invalid sample")
}

// Common values

#[test]
fn a_value_sampled_once_is_never_common() {
    assert_eq!(common_values(&sample(&[("1", 1), ("2", 1)], &[])), [""; 0]);
}

#[test]
fn a_value_sampled_twice_is_common_in_a_small_sample() {
    assert_eq!(common_values(&sample(&[("1", 2), ("2", 1)], &[])), ["1"]);
}

#[test]
fn a_common_value_holds_a_thousandth_of_the_ordinary_rows() {
    // The value 0 has 2 of the rows: common exactly when 2 x 1000 >= S.
    for (ordinary, common) in [(2000, vec!["0"]), (2001, vec![])] {
        let mut owned = vec![("0".to_owned(), 2)];
        owned.extend(singles(1, ordinary - 2));
        let data = sample(&pairs(&owned), &[("sample_null_rows", "5000")]);
        assert_eq!(common_values(&data), common, "{ordinary}");
    }
}

#[test]
fn special_and_null_rows_do_not_count_towards_the_threshold() {
    // S = 2000 with NaN and NULL rows on top, so 0 with 2 rows is still common.
    let mut owned = vec![("0".to_owned(), 2)];
    owned.extend(singles(1, 1998));
    owned.push(("NaN".to_owned(), 500));
    let data = sample(
        &pairs(&owned),
        &[
            ("type", r#""numeric""#),
            ("scale", "0"),
            ("sample_null_rows", "9"),
        ],
    );
    assert_eq!(common_values(&data), ["0"]);
}

#[test]
fn common_values_are_in_order_of_ordinal_with_scaled_rows() {
    let data = sample(
        &[("10", 3), ("-5", 2), ("2", 4)],
        &[("rows_estimate", "90")],
    );
    assert_eq!(
        field(&converted(&data), "mcv"),
        &parsed(
            r#"[{"value": "-5", "rows": 20}, {"value": "2", "rows": 40}, {"value": "10", "rows": 30}]"#
        )
    );
}

#[test]
fn a_common_value_keeps_at_least_one_row() {
    let data = sample(
        &[("4", 2)],
        &[("sample_null_rows", "1000000"), ("rows_estimate", "1")],
    );
    assert_eq!(
        field(&converted(&data), "mcv"),
        &parsed(r#"[{"value": "4", "rows": 1}]"#)
    );
}

// Scaling

#[test]
fn counts_scale_to_the_nearest_row_half_up() {
    let cases = [
        (5, 1, 2, 3),       // 2.5 rounds up
        (7, 1, 4, 2),       // 1.75 rounds up
        (5, 1, 4, 1),       // 1.25 rounds down
        (1000, 2, 15, 133), // the example of spec section 14
        (INT64_MAX, 3, 3, INT64_MAX),
        (0, 3, 3, 0),
    ];
    for (total, nulls, size, expected) in cases {
        let (total_text, nulls_text) = (total.to_string(), nulls.to_string());
        let data = sample(
            &pairs(&singles(1, size - nulls)),
            &[
                ("sample_null_rows", &nulls_text),
                ("rows_estimate", &total_text),
            ],
        );
        assert_eq!(
            field(&converted(&data), "null_rows"),
            &Value::from(expected),
            "{total}, {nulls}, {size}"
        );
    }
}

#[test]
fn an_empty_sample_has_no_rows_whatever_the_estimate() {
    let result = converted(&sample(&[], &[("rows_estimate", "1000000")]));
    assert_eq!(field(&result, "null_rows"), &Value::from(0));
    assert_eq!(field(&result, "mcv"), &parsed("[]"));
    assert_eq!(field(&result, "histogram"), &Value::Null);
    assert_eq!(
        field(&result, "source"),
        &parsed(
            r#"{"statistics": "sample", "rows_estimate": 1000000, "sample_rows": 0,
                "unestimated_rows": 0, "warnings": []}"#
        )
    );
}

#[test]
fn when_every_row_was_read_the_counts_are_exact() {
    let data = sample(
        &[("7", 5), ("9", 1), ("8", 1)],
        &[("sample_null_rows", "3")],
    );
    let result = converted(&data);
    assert_eq!(field(&result, "null_rows"), &Value::from(3));
    assert_eq!(
        field(&result, "mcv"),
        &parsed(r#"[{"value": "7", "rows": 5}]"#)
    );
    assert_eq!(
        field(&result, "histogram"),
        &parsed(r#"{"bounds": ["8", "9"], "rows": 2}"#)
    );
}

// The histogram

#[test]
fn without_ordinary_values_left_there_is_no_histogram() {
    let data = sample(&[("1", 2), ("2", 3)], &[("sample_null_rows", "4")]);
    assert_eq!(histogram(&data), Value::Null);
}

#[test]
fn a_short_list_gives_one_bound_per_value() {
    let cases: [(Pairs, &[&str]); 3] = [
        (&[("5", 1)], &["5", "5"]),
        (&[("5", 1), ("6", 1)], &["5", "6"]),
        (&[("5", 1), ("6", 1), ("7", 1)], &["5", "6", "7"]),
    ];
    for (values, bounds) in cases {
        assert_eq!(bounds_of(&histogram(&sample(values, &[]))), bounds);
    }
}

#[test]
fn a_value_sampled_more_than_once_but_not_common_repeats_in_the_list() {
    // S = 10000 and 9 x 1000 < S, so 0 is not common. -100 is, which leaves the
    // list 0 (9 times), 1, ..., 2991: h = 3000, and bound j is at j x 2999 / 1000.
    let mut owned = vec![("-100".to_owned(), 7000), ("0".to_owned(), 9)];
    owned.extend(singles(1, 2991));
    let bounds = bounds_of(&histogram(&sample(&pairs(&owned), &[])));
    assert_eq!(bounds[..5], ["0", "0", "0", "0", "3"]);
}

#[test]
fn the_histogram_has_at_most_a_thousand_buckets() {
    for count in [RESOLUTION, RESOLUTION + 1, RESOLUTION + 2, 3 * RESOLUTION] {
        let bounds: Vec<i128> = bounds_of(&histogram(&sample(&pairs(&singles(0, count)), &[])))
            .iter()
            .map(|bound| bound.parse().unwrap())
            .collect();
        let buckets = RESOLUTION.min(count - 1);
        // Bound j is the value at position j x (h - 1) / m, and the values are 0 to h - 1.
        let expected: Vec<i128> = (0..=buckets).map(|j| j * (count - 1) / buckets).collect();
        assert_eq!(bounds, expected, "{count}");
    }
}

#[test]
fn the_histogram_holds_the_rows_the_other_parts_leave() {
    let data = sample(
        &[("1", 3), ("8", 1), ("9", 1), ("NaN", 1)],
        &[
            ("type", r#""numeric""#),
            ("scale", "0"),
            ("sample_null_rows", "1"),
            ("rows_estimate", "70"),
        ],
    );
    // n = 7, so a sampled row stands for 10: NULL 10, 1 has 30, NaN 10 left out.
    assert_eq!(
        histogram(&data),
        parsed(r#"{"bounds": ["8", "9"], "rows": 20}"#)
    );
}

#[test]
fn the_histogram_rows_are_never_negative() {
    // T = 1: the common values keep 1 row each, which leaves less than nothing.
    let data = sample(&[("1", 2), ("2", 2), ("9", 1)], &[("rows_estimate", "1")]);
    assert_eq!(
        histogram(&data),
        parsed(r#"{"bounds": ["9", "9"], "rows": 0}"#)
    );
}

#[test]
fn huge_counts_are_exact() {
    // 1001 values of 2^50 rows each: none is common, and the last bound is at
    // position 1001 x 2^50 - 1, which a float would round up past the end.
    let owned: Vec<(String, i128)> = (0..1001)
        .map(|value| (value.to_string(), 1 << 50))
        .collect();
    let found = histogram(&sample(&pairs(&owned), &[]));
    assert_eq!(bounds_of(&found)[999..], ["999", "1000"]);
    assert_eq!(found.get("rows"), Some(&Value::from(1001 * (1 << 50))));
}

#[test]
fn the_order_of_the_sample_does_not_matter() {
    let mut owned = vec![("0".to_owned(), 7)];
    owned.extend(singles(1, 3000));
    owned.push(("-4".to_owned(), 1));
    owned.push(("5000".to_owned(), 9));
    let fields = [("rows_estimate", "10000000")];
    let expected = converted(&sample(&pairs(&owned), &fields));
    // A fixed shuffle: every 7th pair first, then the rest, reversed.
    let (sevenths, rest): (Vec<_>, Vec<_>) = owned
        .iter()
        .cloned()
        .enumerate()
        .partition(|(at, _)| at % 7 == 0);
    let shuffled: Vec<(String, i128)> = sevenths
        .into_iter()
        .chain(rest.into_iter().rev())
        .map(|(_, pair)| pair)
        .collect();
    assert_ne!(shuffled, owned);
    assert_eq!(converted(&sample(&pairs(&shuffled), &fields)), expected);
}

// Special values and the result

#[test]
fn special_values_are_left_out_with_a_warning() {
    let data = sample(
        &[("NaN", 2), ("-Infinity", 1), ("1.5", 1)],
        &[
            ("type", r#""numeric""#),
            ("scale", "1"),
            ("rows_estimate", "40"),
        ],
    );
    let result = converted(&data);
    assert_eq!(
        field(&result, "histogram"),
        &parsed(r#"{"bounds": ["1.5", "1.5"], "rows": 10}"#)
    );
    let source = field(&result, "source");
    assert_eq!(source.get("unestimated_rows"), Some(&Value::from(30)));
    assert_eq!(
        source.get("warnings"),
        Some(&parsed(
            r#"[{"code": "special_values_left_out", "rows": 30, "message": "about 30 rows at special values such as infinity are left out of the estimates; the predicates still read them"}]"#
        ))
    );
}

#[test]
fn a_special_value_scaled_to_no_rows_gives_no_warning() {
    let data = sample(
        &[("infinity", 1)],
        &[("type", r#""date""#), ("rows_estimate", "0")],
    );
    let result = converted(&data);
    let source = field(&result, "source");
    assert_eq!(source.get("unestimated_rows"), Some(&Value::from(0)));
    assert_eq!(source.get("warnings"), Some(&parsed("[]")));
}

#[test]
fn the_result_is_a_planner_input_in_the_order_of_section_1() {
    let data = sample(
        &[("1.5", 1)],
        &[
            ("type", r#""numeric""#),
            ("scale", "2"),
            ("column", r#""amount""#),
            ("partitions", "9"),
        ],
    );
    let result = converted(&data);
    let keys: Vec<&str> = result.as_object().unwrap().keys().collect();
    assert_eq!(
        keys,
        [
            "column",
            "type",
            "scale",
            "partitions",
            "null_rows",
            "mcv",
            "histogram",
            "source"
        ]
    );
    let copied: Vec<&Value> = ["column", "type", "scale", "partitions"]
        .iter()
        .map(|key| field(&result, key))
        .collect();
    assert_eq!(
        copied,
        [
            &Value::from("amount"),
            &Value::from("numeric"),
            &Value::from(2),
            &Value::from(9)
        ]
    );
    assert_eq!(
        field(&result, "histogram"),
        &parsed(r#"{"bounds": ["1.50", "1.50"], "rows": 1}"#)
    );
    assert!(converted(&sample(&[("1", 1)], &[])).get("scale").is_none());
}

// Errors

#[test]
fn a_malformed_sample_is_invalid_input() {
    let with_entries = |text: &str| sample(&[], &[("sample_values", text)]);
    let cases = [
        (parsed("[]"), "the sample must be a JSON object"),
        (
            parsed("{}"),
            "the sample is missing column, type, partitions, rows_estimate, sample_null_rows, sample_values",
        ),
        (
            sample(&[], &[("extra", "1"), ("more", "2")]),
            "the sample has unknown fields: extra, more",
        ),
        (sample(&[], &[("type", "4")]), "type must be a JSON string"),
        (
            sample(&[], &[("scale", r#""2""#)]),
            "scale must be a JSON integer",
        ),
        (
            sample(&[], &[("rows_estimate", "true")]),
            "rows_estimate must be a JSON integer",
        ),
        (
            sample(&[], &[("rows_estimate", "1.0")]),
            "rows_estimate must be a JSON integer",
        ),
        (
            sample(
                &[],
                &[("sample_null_rows", r#""1""#), ("rows_estimate", "1")],
            ),
            "sample_null_rows must be a JSON integer",
        ),
        (with_entries("{}"), ENTRIES),
        (with_entries(r#"["1"]"#), ENTRIES),
        (with_entries(r#"[{"value": "1"}]"#), ENTRIES),
        (
            with_entries(r#"[{"value": "1", "rows": 1, "x": 0}]"#),
            ENTRIES,
        ),
        (with_entries(r#"[{"value": 1, "rows": 1}]"#), ENTRIES),
        (with_entries(r#"[{"value": "1", "rows": true}]"#), ENTRIES),
        (with_entries(r#"[{"value": "1", "rows": 1.0}]"#), ENTRIES),
    ];
    for (data, message) in cases {
        let found = error(&data);
        assert_eq!(
            (found.code(), found.message()),
            (ErrorCode::InvalidInput, message)
        );
    }
}

#[test]
fn counts_out_of_range_are_invalid_rows() {
    let negative = "rows_estimate and sample_null_rows must not be negative";
    let no_rows = "every sampled value needs at least 1 row";
    let too_large = "rows_estimate and the sample size must be at most 9223372036854775807";
    let max = INT64_MAX.to_string();
    let cases = [
        (sample(&[], &[("rows_estimate", "-1")]), negative),
        (sample(&[], &[("sample_null_rows", "-1")]), negative),
        (sample(&[("1", 0)], &[]), no_rows),
        (sample(&[("1", -1), ("2", 2)], &[]), no_rows),
        (
            sample(&[], &[("rows_estimate", "9223372036854775808")]),
            too_large,
        ),
        (
            sample(
                &[("1", INT64_MAX)],
                &[("sample_null_rows", "1"), ("rows_estimate", "1")],
            ),
            too_large,
        ),
        // Counts beyond i128, which JSON allows.
        (
            sample(&[], &[("rows_estimate", &format!("{max}{max}"))]),
            too_large,
        ),
        (
            sample(&[], &[("sample_null_rows", &format!("-{max}{max}"))]),
            negative,
        ),
    ];
    for (data, message) in cases {
        let found = error(&data);
        assert_eq!(
            (found.code(), found.message()),
            (ErrorCode::InvalidRows, message)
        );
    }
}

#[test]
fn the_largest_sample_is_valid() {
    let data = sample(
        &[("1", INT64_MAX - 1)],
        &[("sample_null_rows", "1"), ("rows_estimate", "1")],
    );
    assert_eq!(
        field(&converted(&data), "mcv"),
        &parsed(r#"[{"value": "1", "rows": 1}]"#)
    );
}

#[test]
fn a_value_sampled_twice_is_a_duplicate() {
    let cases = [
        (
            sample(
                &[("1.5", 1), ("1.50", 2)],
                &[("type", r#""numeric""#), ("scale", "2")],
            ),
            "1.50 appears more than once in sample_values",
        ),
        (
            sample(
                &[("infinity", 1), ("2026-01-01", 1), ("infinity", 1)],
                &[("type", r#""date""#)],
            ),
            "infinity appears more than once in sample_values",
        ),
    ];
    for (data, message) in cases {
        let found = error(&data);
        assert_eq!(
            (found.code(), found.message()),
            (ErrorCode::DuplicateValue, message)
        );
    }
}

#[test]
fn values_and_results_are_checked_as_in_section_9() {
    let max = INT64_MAX.to_string();
    let cases = [
        (sample(&[("infinity", 1)], &[]), ErrorCode::InvalidValue),
        (
            sample(&[("Infinity", 1)], &[("type", r#""date""#)]),
            ErrorCode::InvalidValue,
        ),
        (
            sample(&[("NaN", 1)], &[("type", r#""timestamp""#)]),
            ErrorCode::InvalidValue,
        ),
        (
            sample(&[("40000", 1)], &[("type", r#""int2""#)]),
            ErrorCode::ValueOutOfRange,
        ),
        (
            sample(&[], &[("type", r#""text""#)]),
            ErrorCode::UnsupportedType,
        ),
        (
            sample(&[], &[("type", r#""numeric""#)]),
            ErrorCode::UnsupportedType,
        ),
        (
            sample(&[], &[("partitions", "0")]),
            ErrorCode::InvalidPartitions,
        ),
        (
            sample(&[], &[("column", r#""""#)]),
            ErrorCode::InvalidColumn,
        ),
        (
            sample(
                &[("5", 2)],
                &[("sample_null_rows", "2"), ("rows_estimate", &max)],
            ),
            ErrorCode::InvalidRows,
        ),
    ];
    for (data, code) in cases {
        assert_eq!(error(&data).code(), code, "{}", data.to_json(None).unwrap());
    }
}

/// A scale beyond i64, which JSON allows, has the message the planner input
/// checks give it.
#[test]
fn a_scale_beyond_i64_is_an_unsupported_type() {
    let huge = "99999999999999999999";
    let found = error(&sample(&[], &[("type", r#""numeric""#), ("scale", huge)]));
    assert_eq!(
        (found.code(), found.message()),
        (
            ErrorCode::UnsupportedType,
            "numeric scale 99999999999999999999 is outside 0 to 1000"
        )
    );
}
