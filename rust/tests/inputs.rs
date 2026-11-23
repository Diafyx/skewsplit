//! The planner input checks (spec sections 1 and 9), with the same cases as
//! the Python package's `python/tests/unit/test_inputs.py`, and every fixture.

use std::path::Path;

use skewsplit::ErrorCode;
use skewsplit::inputs::{PlanInput, parse_input};
use skewsplit::json::{self, Value};

// The input of spec section 1.
const BASE: &str = r#"{
    "column": "customer_id",
    "type": "int4",
    "partitions": 4,
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400}
}"#;

/// Keys to set to the JSON text given, or to remove for `None`.
type Fields<'a> = &'a [(&'a str, Option<&'a str>)];

/// The base input with each key set to the JSON text given, or removed for `None`.
fn changed(fields: Fields) -> Value {
    let mut data = json::parse(BASE).unwrap();
    let Value::Object(object) = &mut data else {
        unreachable!("the base input is an object");
    };
    for &(key, value) in fields {
        match value {
            Some(text) => object.insert(key, json::parse(text).unwrap()),
            None => {
                object.remove(key);
            }
        }
    }
    data
}

fn parsed(fields: Fields) -> PlanInput {
    parse_input(&changed(fields)).expect("a valid input")
}

#[test]
fn parse_converts_values_to_sorted_ordinals() {
    let input = parsed(&[(
        "mcv",
        Some(r#"[{"value": "42", "rows": 500}, {"value": "7", "rows": 60}]"#),
    )]);
    assert_eq!(input.mcv(), [(7, 60), (42, 500)]);
    assert_eq!(input.bounds(), [1, 250, 500, 750, 1000]);
    assert_eq!(
        (
            input.column(),
            input.partitions(),
            input.null_rows(),
            input.histogram_rows()
        ),
        ("customer_id", 4, 100, 400)
    );
    assert_eq!(input.codec().type_name(), "int4");
    assert!(input.source().is_null());
}

#[test]
fn parse_accepts_no_histogram_a_source_and_a_numeric_scale() {
    let input = parsed(&[
        ("type", Some(r#""numeric""#)),
        ("scale", Some("1")),
        ("histogram", Some("null")),
        ("source", Some(r#"{"a": [1]}"#)),
    ]);
    assert_eq!((input.bounds(), input.histogram_rows()), (&[][..], 0));
    assert_eq!(input.source(), &json::parse(r#"{"a": [1]}"#).unwrap());
    assert_eq!(input.mcv(), [(70, 60), (420, 500)]);
}

#[test]
fn parse_accepts_repeated_bounds_and_the_largest_total() {
    let rows = i128::from(i64::MAX) - 660;
    let histogram = format!(r#"{{"bounds": ["1", "5", "5", "9"], "rows": {rows}}}"#);
    assert_eq!(
        parsed(&[("histogram", Some(&histogram))]).bounds(),
        [1, 5, 5, 9]
    );
}

#[test]
fn wrong_shape_is_invalid_input() {
    let cases: [(Value, &str); 21] = [
        (json::parse("[]").unwrap(), "must be a JSON object"),
        (changed(&[("histogram", None)]), "missing histogram"),
        (
            changed(&[("column", None), ("mcv", None)]),
            "missing column, mcv",
        ),
        (
            changed(&[("table", Some(r#""t""#))]),
            "unknown fields: table",
        ),
        (
            changed(&[("column", Some("1"))]),
            "column must be a JSON string",
        ),
        (
            changed(&[("type", Some("null"))]),
            "type must be a JSON string",
        ),
        (
            changed(&[("partitions", Some("4.0"))]),
            "partitions must be a JSON integer",
        ),
        (
            changed(&[("partitions", Some("true"))]),
            "partitions must be a JSON integer",
        ),
        (
            changed(&[("null_rows", Some(r#""100""#))]),
            "null_rows must be a JSON integer",
        ),
        (
            changed(&[("type", Some(r#""numeric""#)), ("scale", Some(r#""2""#))]),
            "scale must be a JSON integer",
        ),
        (
            changed(&[("mcv", Some(r#"{"value": "7"}"#))]),
            "mcv must be a list",
        ),
        (
            changed(&[("mcv", Some(r#"["7"]"#))]),
            "each mcv entry must be an object",
        ),
        (
            changed(&[("mcv", Some(r#"[{"value": "7"}]"#))]),
            "an mcv entry is missing rows",
        ),
        (
            changed(&[("mcv", Some(r#"[{"value": "7", "rows": 1, "share": 0.1}]"#))]),
            "unknown fields: share",
        ),
        (
            changed(&[("mcv", Some(r#"[{"value": 7, "rows": 1}]"#))]),
            "value must be a JSON string",
        ),
        (
            changed(&[("mcv", Some(r#"[{"value": "7", "rows": false}]"#))]),
            "rows must be a JSON integer",
        ),
        (
            changed(&[("histogram", Some(r#"["1", "2"]"#))]),
            "histogram must be an object or null",
        ),
        (
            changed(&[("histogram", Some(r#"{"bounds": ["1", "2"]}"#))]),
            "the histogram is missing rows",
        ),
        (
            changed(&[("histogram", Some(r#"{"bounds": "1,2", "rows": 1}"#))]),
            "bounds must be a list of strings",
        ),
        (
            changed(&[("histogram", Some(r#"{"bounds": [1, 2], "rows": 1}"#))]),
            "bounds must be a list of strings",
        ),
        (
            changed(&[("histogram", Some(r#"{"bounds": ["1", "2"], "rows": 1.5}"#))]),
            "rows must be a JSON integer",
        ),
    ];
    for (data, message) in cases {
        let error = parse_input(&data).expect_err("the input is refused");
        assert_eq!(error.code(), ErrorCode::InvalidInput, "{error}");
        assert!(error.message().contains(message), "{error}");
    }
}

#[test]
fn each_problem_has_its_error_code() {
    let total = |rows: i128| format!("{rows}");
    let largest = total(i128::from(i64::MAX) - 960);
    let too_large = total(i128::from(i64::MAX) - 959);
    let cases: [(Fields, Option<ErrorCode>); 21] = [
        (&[("column", Some(r#""""#))], Some(ErrorCode::InvalidColumn)),
        (
            &[("column", Some(r#""a\u0000b""#))],
            Some(ErrorCode::InvalidColumn),
        ),
        (
            &[("type", Some(r#""uuid""#))],
            Some(ErrorCode::UnsupportedType),
        ),
        (
            &[("type", Some(r#""numeric""#))],
            Some(ErrorCode::UnsupportedType),
        ),
        (&[("scale", Some("0"))], Some(ErrorCode::UnsupportedType)),
        (
            &[("partitions", Some("0"))],
            Some(ErrorCode::InvalidPartitions),
        ),
        (
            &[("partitions", Some("10001"))],
            Some(ErrorCode::InvalidPartitions),
        ),
        (&[("null_rows", Some("-1"))], Some(ErrorCode::InvalidRows)),
        (
            &[("histogram", Some(r#"{"bounds": ["1", "2"], "rows": -1}"#))],
            Some(ErrorCode::InvalidRows),
        ),
        (
            &[("mcv", Some(r#"[{"value": "7", "rows": 0}]"#))],
            Some(ErrorCode::InvalidRows),
        ),
        (
            &[("mcv", Some(r#"[{"value": "7", "rows": -5}]"#))],
            Some(ErrorCode::InvalidRows),
        ),
        (&[("null_rows", Some(&largest))], None),
        (
            &[("null_rows", Some(&too_large))],
            Some(ErrorCode::InvalidRows),
        ),
        (
            &[("mcv", Some(r#"[{"value": "07", "rows": 1}]"#))],
            Some(ErrorCode::InvalidValue),
        ),
        (
            &[("histogram", Some(r#"{"bounds": ["1", "x"], "rows": 1}"#))],
            Some(ErrorCode::InvalidValue),
        ),
        (
            &[("mcv", Some(r#"[{"value": "2147483648", "rows": 1}]"#))],
            Some(ErrorCode::ValueOutOfRange),
        ),
        (
            &[(
                "mcv",
                Some(r#"[{"value": "7", "rows": 1}, {"value": "7", "rows": 2}]"#),
            )],
            Some(ErrorCode::DuplicateValue),
        ),
        (
            &[("histogram", Some(r#"{"bounds": ["1"], "rows": 1}"#))],
            Some(ErrorCode::InvalidHistogram),
        ),
        (
            &[("histogram", Some(r#"{"bounds": [], "rows": 0}"#))],
            Some(ErrorCode::InvalidHistogram),
        ),
        (
            &[(
                "histogram",
                Some(r#"{"bounds": ["1", "3", "2"], "rows": 1}"#),
            )],
            Some(ErrorCode::InvalidHistogram),
        ),
        (
            &[("histogram", Some(r#"{"bounds": ["1", "42"], "rows": 1}"#))],
            Some(ErrorCode::InvalidHistogram),
        ),
    ];
    for (fields, code) in cases {
        let result = parse_input(&changed(fields));
        assert_eq!(result.err().map(|error| error.code()), code, "{fields:?}");
    }
}

#[test]
fn equal_numeric_values_written_differently_are_duplicates() {
    let error = parse_input(&changed(&[
        ("type", Some(r#""numeric""#)),
        ("scale", Some("2")),
        (
            "mcv",
            Some(r#"[{"value": "12.5", "rows": 1}, {"value": "12.50", "rows": 2}]"#),
        ),
        ("histogram", Some("null")),
    ]))
    .unwrap_err();
    assert_eq!(error.code(), ErrorCode::DuplicateValue);
    assert!(
        error.message().contains("12.50 appears more than once"),
        "{error}"
    );
}

#[test]
fn messages_name_the_problem_values() {
    let cases = [
        (
            r#"{"bounds": ["1", "3", "2"], "rows": 1}"#,
            "bounds 3 and 2 are out of order",
        ),
        (
            r#"{"bounds": ["7", "42"], "rows": 1}"#,
            "the bound 7 is also a common value",
        ),
    ];
    for (histogram, message) in cases {
        let error = parse_input(&changed(&[("histogram", Some(histogram))])).unwrap_err();
        assert!(error.message().contains(message), "{error}");
    }
}

// The messages that Python's parse_input gives for the same inputs.
#[test]
fn messages_are_python_s() {
    let cases: [(Fields, &str); 8] = [
        (
            &[(
                "partitions",
                Some("123456789012345678901234567890123456789012"),
            )],
            "partitions is 123456789012345678901234567890123456789012; it must be 1 to 10000",
        ),
        (
            &[(
                "null_rows",
                Some("-123456789012345678901234567890123456789012"),
            )],
            "row counts must not be negative",
        ),
        (
            &[(
                "mcv",
                Some(r#"[{"value": "7", "rows": 123456789012345678901234567890123456789012}]"#),
            )],
            "the total row count exceeds 9223372036854775807",
        ),
        (
            &[
                ("type", Some(r#""numeric""#)),
                ("scale", Some("123456789012345678901234567890")),
            ],
            "numeric scale 123456789012345678901234567890 is outside 0 to 1000",
        ),
        (
            &[("scale", Some("-123456789012345678901234567890"))],
            "scale is only for numeric, not int4",
        ),
        (
            &[
                ("type", Some(r#""text""#)),
                ("scale", Some("123456789012345678901234567890")),
            ],
            "type 'text' is not supported; expected one of int2, int4, int8, date, timestamp, timestamptz, numeric",
        ),
        (
            &[
                ("zeta", Some("1")),
                ("alpha", Some("2")),
                ("Émile", Some("3")),
            ],
            "the input has unknown fields: alpha, zeta, Émile",
        ),
        (
            &[("column", Some(r#""""#))],
            "the column name must not be empty or contain U+0000",
        ),
    ];
    for (fields, message) in cases {
        let error = parse_input(&changed(fields)).unwrap_err();
        assert_eq!(error.message(), message);
    }
}

fn fixture_inputs() -> Vec<(String, Value, Value)> {
    let folder = Path::new(env!("CARGO_MANIFEST_DIR")).join("../spec/fixtures");
    let mut paths: Vec<_> = std::fs::read_dir(folder)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .collect();
    paths.sort();
    paths
        .into_iter()
        .map(|path| {
            let mut fixture = json::parse(&std::fs::read_to_string(&path).unwrap()).unwrap();
            let Value::Object(object) = &mut fixture else {
                unreachable!("a fixture is an object");
            };
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            (
                name,
                object.remove("input").unwrap(),
                object.remove("expected").unwrap(),
            )
        })
        .collect()
}

// spec/fixtures: an error fixture fails with its code, and every other
// fixture's input is valid, with the rows its plan counts.
#[test]
fn every_fixture_input_is_checked_as_its_fixture_says() {
    let fixtures = fixture_inputs();
    assert!(fixtures.len() >= 40);
    for (name, input, expected) in fixtures {
        let result = parse_input(&input);
        if let Some(error) = expected.get("error") {
            let code = result.expect_err(&name).code();
            assert_eq!(
                Some(code.as_str()),
                error.get("code").and_then(Value::as_str),
                "{name}"
            );
        } else {
            let input = result.unwrap_or_else(|error| panic!("{name}: {error}"));
            let rows = expected.get("rows").unwrap();
            let non_null: i128 =
                input.mcv().iter().map(|&(_, rows)| rows).sum::<i128>() + input.histogram_rows();
            assert_eq!(
                rows.get("null").and_then(Value::as_i128),
                Some(input.null_rows())
            );
            assert_eq!(
                rows.get("non_null").and_then(Value::as_i128),
                Some(non_null),
                "{name}"
            );
            assert_eq!(
                expected.get("type").and_then(Value::as_str),
                Some(input.codec().type_name())
            );
        }
    }
}
