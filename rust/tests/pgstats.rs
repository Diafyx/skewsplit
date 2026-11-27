//! Converting `pg_stats` into a planner input (spec section 13): each check at
//! its edges, with the messages of the Python core, and numbers at the edges
//! of the accepted form. Every expected result here is what Python's
//! `from_pg_stats` gives. `tests/fixtures.rs` runs every fixture in
//! `spec/fixtures/pg-stats/`.

use skewsplit::ErrorCode;
use skewsplit::json::{self, Value};
use skewsplit::pgstats::from_pg_stats;

/// The first example of section 13.
const EXAMPLE: &str = r#"{"column": "c", "type": "int4", "partitions": 4, "reltuples": ["2000"],
    "null_frac": "0.05", "most_common_vals": "{42,7}", "most_common_freqs": "{0.25,0.03}",
    "histogram_bounds": "{1,250,500,750,1000}", "modified_since_analyze": 150}"#;
const NOT_A_NUMBER: &str = "is not a non-negative decimal number";

/// The example with each field of `changes`, a JSON object, set to its value.
fn stats(changes: &str) -> Value {
    let mut data = json::parse(EXAMPLE).unwrap();
    for (key, value) in json::parse(changes).unwrap().as_object().unwrap().iter() {
        data = with(data, key, value.clone());
    }
    data
}

fn with(mut data: Value, key: &str, value: Value) -> Value {
    if let Value::Object(fields) = &mut data {
        fields.insert(key, value);
    }
    data
}

fn without(mut data: Value, keys: &[&str]) -> Value {
    if let Value::Object(fields) = &mut data {
        for key in keys {
            fields.remove(key);
        }
    }
    data
}

/// The converted input as compact JSON.
fn converted(data: &Value) -> String {
    from_pg_stats(data)
        .expect("valid statistics")
        .to_json(None)
        .unwrap()
}

fn error(data: &Value) -> (ErrorCode, String) {
    let error = from_pg_stats(data).expect_err("invalid statistics");
    (error.code(), error.message().to_owned())
}

fn input_error(message: &str) -> (ErrorCode, String) {
    (ErrorCode::InvalidInput, message.to_owned())
}

fn statistics_error(message: &str) -> (ErrorCode, String) {
    (ErrorCode::InvalidStatistics, message.to_owned())
}

#[test]
fn the_first_example_of_section_13() {
    assert_eq!(
        converted(&stats("{}")),
        r#"{"column": "c", "type": "int4", "partitions": 4, "null_rows": 100, "mcv": [{"value": "42", "rows": 500}, {"value": "7", "rows": 60}], "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 1340}, "source": {"statistics": "pg_stats", "rows_estimate": 2000, "unestimated_rows": 0, "modified_since_analyze": 150, "warnings": []}}"#
    );
}

// The shape of the input

#[test]
fn fields_that_are_missing_unknown_or_of_the_wrong_type() {
    assert_eq!(
        error(&json::parse("[]").unwrap()),
        input_error("the statistics must be a JSON object")
    );
    assert_eq!(
        error(&without(stats("{}"), &["null_frac", "histogram_bounds"])),
        input_error("the statistics are missing null_frac, histogram_bounds")
    );
    assert_eq!(
        error(&stats(r#"{"zeta": 1, "alpha": 2}"#)),
        input_error("the statistics have unknown fields: alpha, zeta")
    );
    for (changes, message) in [
        (r#"{"type": 5}"#, "type must be a JSON string"),
        (
            r#"{"type": "numeric", "scale": "2"}"#,
            "scale must be a JSON integer",
        ),
        (
            r#"{"reltuples": "2000"}"#,
            "reltuples must be a list of strings",
        ),
        (
            r#"{"reltuples": ["1", 2]}"#,
            "reltuples must be a list of strings",
        ),
        (r#"{"null_frac": 0}"#, "null_frac must be a JSON string"),
        (
            r#"{"most_common_freqs": 5}"#,
            "most_common_freqs must be a JSON string or null",
        ),
        (
            r#"{"histogram_bounds": []}"#,
            "histogram_bounds must be a JSON string or null",
        ),
        (
            r#"{"modified_since_analyze": 1.5}"#,
            "modified_since_analyze must be a JSON integer or null",
        ),
        (
            r#"{"modified_since_analyze": true}"#,
            "modified_since_analyze must be a JSON integer or null",
        ),
    ] {
        assert_eq!(error(&stats(changes)), input_error(message), "{changes}");
    }
}

#[test]
fn checks_after_the_shape_in_python_s_order() {
    assert_eq!(
        error(&stats(r#"{"modified_since_analyze": -1, "type": "text"}"#)),
        statistics_error("modified_since_analyze must not be negative")
    );
    assert_eq!(
        error(&stats(r#"{"type": "text", "reltuples": []}"#)),
        (
            ErrorCode::UnsupportedType,
            "type 'text' is not supported; expected one of int2, int4, int8, date, timestamp, timestamptz, numeric".to_owned()
        )
    );
    assert_eq!(
        error(&stats(r#"{"reltuples": [], "null_frac": "x"}"#)),
        statistics_error("reltuples needs at least one row count")
    );
}

// Numbers

#[test]
fn a_number_outside_the_form_shows_its_text_as_python_s_repr() {
    let wide = char::from_u32(0xe9).unwrap().to_string();
    for (key, text, shown) in [
        ("reltuples", "1E+05", "'1E+05'".to_owned()),
        (
            "reltuples",
            &"1".repeat(41),
            format!("'{}'", "1".repeat(40)),
        ),
        (
            "reltuples",
            &wide.repeat(41),
            format!("'{}'", wide.repeat(40)),
        ),
        ("null_frac", "it's", r#""it's""#.to_owned()),
        ("null_frac", "1\x002\n", r"'1\x002\n'".to_owned()),
    ] {
        let value = if key == "reltuples" {
            Value::Array(vec![Value::from("2000"), Value::from(text)])
        } else {
            Value::from(text)
        };
        assert_eq!(
            error(&with(stats("{}"), key, value)),
            statistics_error(&format!("{key} {shown} {NOT_A_NUMBER}")),
            "{text:?}"
        );
    }
}

#[test]
fn a_fraction_above_one() {
    assert_eq!(
        error(&stats(r#"{"null_frac": "1.5"}"#)),
        statistics_error("null_frac '1.5' is above 1")
    );
    assert_eq!(
        error(&stats(
            r#"{"most_common_freqs": "{0.25,1.0000000000000000000000000000000001}"}"#
        )),
        statistics_error("a frequency '1.0000000000000000000000000000000001' is above 1")
    );
}

#[test]
fn the_longest_text_and_the_smallest_exponent() {
    let longest = format!("1.{}e+05", "0".repeat(34));
    assert_eq!(longest.len(), 40);
    let data = with(
        stats(
            r#"{"null_frac": "9.99999999999999999999999999999999e-999",
                "most_common_vals": null, "most_common_freqs": null,
                "histogram_bounds": "{1,2}", "modified_since_analyze": null}"#,
        ),
        "reltuples",
        Value::Array(vec![Value::from(longest.as_str())]),
    );
    assert_eq!(
        converted(&data),
        r#"{"column": "c", "type": "int4", "partitions": 4, "null_rows": 0, "mcv": [], "histogram": {"bounds": ["1", "2"], "rows": 100000}, "source": {"statistics": "pg_stats", "rows_estimate": 100000, "unestimated_rows": 0, "modified_since_analyze": null, "warnings": []}}"#
    );
}

#[test]
fn a_tiny_part_of_a_number_still_counts_when_rounding() {
    // T = 2.5 + 10^-999 rounds to 3, and null_frac 0.2 × T just above 0.5
    // rounds to 1.
    let data = stats(
        r#"{"reltuples": ["2.5", "1e-999"], "null_frac": "0.2", "most_common_vals": null,
            "most_common_freqs": null, "histogram_bounds": null,
            "modified_since_analyze": null}"#,
    );
    assert_eq!(
        converted(&data),
        r#"{"column": "c", "type": "int4", "partitions": 4, "null_rows": 1, "mcv": [], "histogram": null, "source": {"statistics": "pg_stats", "rows_estimate": 3, "unestimated_rows": 0, "modified_since_analyze": null, "warnings": []}}"#
    );
}

#[test]
fn counts_far_beyond_i128_are_exact() {
    // 10^999 rows, all at infinity, so the planner input has none.
    let huge = format!("1{}", "0".repeat(999));
    let modified = format!("1{}", "0".repeat(1000));
    let data = stats(&format!(
        r#"{{"type": "date", "reltuples": ["1e+999"], "null_frac": "0",
            "most_common_vals": "{{infinity}}", "most_common_freqs": "{{1}}",
            "histogram_bounds": null, "modified_since_analyze": {modified}}}"#
    ));
    assert_eq!(
        converted(&data),
        format!(
            r#"{{"column": "c", "type": "date", "partitions": 4, "null_rows": 0, "mcv": [], "histogram": null, "source": {{"statistics": "pg_stats", "rows_estimate": {huge}, "unestimated_rows": {huge}, "modified_since_analyze": {modified}, "warnings": [{{"code": "stale_statistics", "modified_rows": {modified}, "rows": {huge}, "message": "{modified} rows changed since the statistics were gathered, more than a tenth of the {huge} rows; run ANALYZE for an accurate plan"}}, {{"code": "special_values_left_out", "rows": {huge}, "message": "{huge} rows at special values such as infinity, or in histogram buckets next to them, are left out of the estimates; the predicates still read them"}}]}}}}"#
        )
    );
    assert_eq!(
        error(&stats(r#"{"reltuples": ["1e+999"]}"#)),
        (
            ErrorCode::InvalidRows,
            "the total row count exceeds 9223372036854775807".to_owned()
        )
    );
}

// Common values and the histogram

#[test]
fn common_values_and_frequencies_pair_up() {
    let both = "most_common_vals and most_common_freqs must both be null or both be arrays";
    for changes in [
        r#"{"most_common_freqs": null}"#,
        r#"{"most_common_vals": null}"#,
    ] {
        assert_eq!(error(&stats(changes)), statistics_error(both), "{changes}");
    }
    assert_eq!(
        error(&stats(r#"{"most_common_freqs": "{0.25}"}"#)),
        statistics_error("2 common values but 1 frequencies")
    );
}

#[test]
fn array_text_and_values_give_their_own_errors() {
    for (changes, code, message) in [
        (
            r#"{"most_common_vals": "{42,7"}"#,
            ErrorCode::InvalidArray,
            "'{42,7' is not array text: elements are separated by ',' and end with '}'",
        ),
        (
            r#"{"most_common_freqs": "{0.25,0.03"}"#,
            ErrorCode::InvalidArray,
            "'{0.25,0.03' is not array text: elements are separated by ',' and end with '}'",
        ),
        (
            r#"{"type": "numeric", "scale": 99999999999999999999}"#,
            ErrorCode::UnsupportedType,
            "numeric scale 99999999999999999999 is outside 0 to 1000",
        ),
        (
            r#"{"histogram_bounds": "{1,NULL}"}"#,
            ErrorCode::InvalidArray,
            "'{1,NULL}' is not array text: pg_stats arrays never hold NULL",
        ),
        (
            r#"{"most_common_vals": "{42,x}"}"#,
            ErrorCode::InvalidValue,
            "'x' is not a valid int4 value: expected an integer such as 42 or -5",
        ),
        (
            r#"{"histogram_bounds": "{1,99999999999}"}"#,
            ErrorCode::ValueOutOfRange,
            "'99999999999' is outside the int4 range, -2147483648 to 2147483647",
        ),
        (
            r#"{"partitions": 0}"#,
            ErrorCode::InvalidPartitions,
            "partitions is 0; it must be 1 to 10000",
        ),
    ] {
        assert_eq!(
            error(&stats(changes)),
            (code, message.to_owned()),
            "{changes}"
        );
    }
}

#[test]
fn a_histogram_needs_two_bounds() {
    for (text, count) in [("{5}", 1), ("{}", 0)] {
        let changes = format!(r#"{{"histogram_bounds": "{text}"}}"#);
        assert_eq!(
            error(&stats(&changes)),
            (
                ErrorCode::InvalidHistogram,
                format!("histogram_bounds has {count} bounds; a histogram needs at least 2")
            )
        );
    }
}

#[test]
fn special_values_only_at_the_ends_of_the_histogram() {
    assert_eq!(
        error(&stats(
            r#"{"type": "date", "most_common_vals": null, "most_common_freqs": null,
                "histogram_bounds": "{2020-01-01,infinity,2021-01-01}"}"#
        )),
        statistics_error("special values can only be the first or last bounds")
    );
    // NaN at 100 rows, and the buckets next to -Infinity and Infinity: 2 of
    // the 4 buckets of the 800 histogram rows.
    let data = stats(
        r#"{"type": "numeric", "scale": 1, "reltuples": ["1000"], "null_frac": "0",
            "most_common_vals": "{NaN,5}", "most_common_freqs": "{0.1,0.1}",
            "histogram_bounds": "{-Infinity,1,2,3,Infinity}", "modified_since_analyze": 0}"#,
    );
    assert_eq!(
        converted(&data),
        r#"{"column": "c", "type": "numeric", "scale": 1, "partitions": 4, "null_rows": 0, "mcv": [{"value": "5.0", "rows": 100}], "histogram": {"bounds": ["1.0", "2.0", "3.0"], "rows": 400}, "source": {"statistics": "pg_stats", "rows_estimate": 1000, "unestimated_rows": 500, "modified_since_analyze": 0, "warnings": [{"code": "special_values_left_out", "rows": 500, "message": "500 rows at special values such as infinity, or in histogram buckets next to them, are left out of the estimates; the predicates still read them"}]}}"#
    );
}

#[test]
fn the_histogram_rows_are_never_negative() {
    // 2000 - 1200 NULL rows - 2 × 1000 common rows is below 0.
    let data = stats(r#"{"null_frac": "0.6", "most_common_freqs": "{0.5,0.5}"}"#);
    assert!(
        converted(&data)
            .contains(r#""histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 0}"#)
    );
}
