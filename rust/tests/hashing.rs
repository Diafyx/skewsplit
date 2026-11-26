//! Hash predicates (spec section 16), with the cases of the Python package's
//! `python/tests/unit/test_hashing.py`: every rule at its edges, and the exact
//! errors. `tests/fixtures.rs` runs every fixture in `spec/fixtures/hash/`.

use skewsplit::ErrorCode;
use skewsplit::hashing::hash_plan;
use skewsplit::json::{self, Value};
use skewsplit::plan::{Kind, Plan, WarningCode};

// The example of spec section 16.
const BASE: &str = r#"{
    "column": "session_id",
    "type": "uuid",
    "partitions": 4,
    "null_rows": 10,
    "non_null_rows": 1000
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

fn planned(fields: Fields) -> Plan {
    hash_plan(&changed(fields)).expect("a valid input")
}

fn hash_sql(column: &str, n: i128, i: i128) -> String {
    format!("((pg_catalog.hashtextextended({column}::text, 0) % {n}) + {n}) % {n} = {i}")
}

fn estimates(result: &Plan) -> Vec<i128> {
    result
        .parts
        .iter()
        .map(|part| part.estimated_rows)
        .collect()
}

fn codes(result: &Plan) -> Vec<&'static str> {
    result
        .warnings
        .iter()
        .map(|warning| warning.code.as_str())
        .collect()
}

#[test]
fn the_example_of_the_spec() {
    let result = planned(&[]);
    let mut expected = vec![r#""session_id" IS NULL"#.to_owned()];
    expected.extend((0..4).map(|i| hash_sql(r#""session_id""#, 4, i)));
    assert_eq!(result.predicates(), expected);
    assert_eq!(estimates(&result), [10, 250, 250, 250, 250]);
    let kinds: Vec<Kind> = result.parts.iter().map(|part| part.kind.clone()).collect();
    assert_eq!(
        kinds,
        [
            Kind::Null,
            Kind::Hash(0),
            Kind::Hash(1),
            Kind::Hash(2),
            Kind::Hash(3)
        ]
    );
    assert_eq!(
        (result.total_rows, result.null_rows, result.skew_milli),
        (1010, 10, Some(990))
    );
    assert_eq!(
        (
            result.column.as_str(),
            result.type_name.as_str(),
            result.scale
        ),
        ("session_id", "uuid", None)
    );
    assert_eq!(
        (result.requested_partitions, result.planned_partitions()),
        (4, 4)
    );
    assert_eq!(result.source, Value::Null);
}

#[test]
fn the_json_of_a_hash_predicate() {
    let result = planned(&[]);
    let predicate = json::parse(
        r#"{
            "kind": "hash",
            "remainder": 0,
            "sql": "((pg_catalog.hashtextextended(\"session_id\"::text, 0) % 4) + 4) % 4 = 0",
            "estimated_rows": 250
        }"#,
    )
    .unwrap();
    assert_eq!(result.parts[1].to_value(), predicate);
    let plan = result.to_value();
    assert!(plan.get("scale").is_none(), "a hash plan has no scale");
    assert_eq!(plan.get("skew_score"), Some(&Value::Float(0.99)));
}

#[test]
fn the_rows_are_spread_like_histogram_buckets() {
    let cases: [(i128, i128, &[i128]); 5] = [
        (4, 10, &[2, 3, 2, 3]),
        (7, 100, &[14, 14, 14, 15, 14, 14, 15]),
        (5, 2, &[0, 0, 1, 0, 1]),
        (3, 0, &[0, 0, 0]),
        (1, 7, &[7]),
    ];
    for (partitions, rows, expected) in cases {
        let (n, r) = (partitions.to_string(), rows.to_string());
        let result = planned(&[
            ("partitions", Some(&n)),
            ("null_rows", Some("0")),
            ("non_null_rows", Some(&r)),
        ]);
        assert_eq!(estimates(&result)[1..], *expected, "{partitions}, {rows}");
        assert_eq!(result.planned_partitions(), partitions);
    }
}

#[test]
fn the_column_is_quoted() {
    let result = planned(&[
        ("column", Some(r#""a\"b""#)),
        ("type", Some(r#""character varying(20)""#)),
    ]);
    assert_eq!(result.predicates()[0], r#""a""b" IS NULL"#);
    assert_eq!(result.predicates()[1], hash_sql(r#""a""b""#, 4, 0));
    assert_eq!(result.type_name, "character varying(20)");
}

#[test]
fn the_source_is_copied() {
    let source = r#"{"statistics": "count", "rows": 1010}"#;
    let result = planned(&[("source", Some(source))]);
    assert_eq!(result.source, json::parse(source).unwrap());
}

#[test]
fn every_hash_plan_warns_that_each_partition_scans_the_table() {
    let result = planned(&[]);
    let [warning] = result.warnings.as_slice() else {
        panic!("one warning, not {:?}", result.warnings);
    };
    assert_eq!((warning.code, warning.rows), (WarningCode::FullScans, None));
    assert_eq!(
        warning.message,
        "each of the 4 hash predicates reads the whole table: no index on the column can serve them, so the read scans the table once per partition"
    );
}

#[test]
fn the_warnings_and_their_order() {
    let cases: [(&str, &str, &[&str]); 4] = [
        // 1 * 5 > 3: one NULL row is more than a fifth of 3 rows.
        ("1", "2", &["full_scans", "nulls_exceed_share"]),
        // 2 * 5 > 10 is false: exactly one partition's share.
        ("2", "8", &["full_scans"]),
        ("3", "7", &["full_scans", "nulls_exceed_share"]),
        ("0", "0", &["full_scans", "no_rows"]),
    ];
    for (null_rows, rows, expected) in cases {
        let result = planned(&[
            ("partitions", Some("5")),
            ("null_rows", Some(null_rows)),
            ("non_null_rows", Some(rows)),
        ]);
        assert_eq!(codes(&result), expected, "{null_rows}, {rows}");
    }
}

#[test]
fn the_warning_messages() {
    let result = planned(&[("null_rows", Some("100")), ("non_null_rows", Some("0"))]);
    let nulls = &result.warnings[1];
    assert_eq!(
        (nulls.rows, nulls.message.as_str()),
        (
            Some(100),
            "100 NULL rows are more than one partition's share"
        )
    );
    let result = planned(&[("null_rows", Some("0")), ("non_null_rows", Some("0"))]);
    assert_eq!(
        result.warnings[1].message,
        "the input shows no rows; the plan still reads rows added later"
    );
}

#[test]
fn the_skew_score() {
    let cases = [
        ("0", "0", None),
        ("100", "0", Some(4000)),
        ("0", "10", Some(1200)),
        ("1", "9223372036854775806", Some(1000)),
    ];
    for (null_rows, rows, skew) in cases {
        let result = planned(&[
            ("null_rows", Some(null_rows)),
            ("non_null_rows", Some(rows)),
        ]);
        assert_eq!(result.skew_milli, skew, "{null_rows}, {rows}");
    }
}

#[test]
fn the_largest_total() {
    let result = planned(&[
        ("partitions", Some("3")),
        ("null_rows", Some("1")),
        ("non_null_rows", Some("9223372036854775806")),
    ]);
    assert_eq!(result.total_rows, 9_223_372_036_854_775_807);
}

#[test]
fn the_partition_limits() {
    for partitions in ["1", "10000"] {
        let result = planned(&[("partitions", Some(partitions))]);
        assert_eq!(result.planned_partitions().to_string(), partitions);
    }
}

#[test]
fn the_report_and_the_snippets_are_python_s() {
    let result = planned(&[]);
    let hashes: Vec<String> = (0..4).map(|i| hash_sql(r#""session_id""#, 4, i)).collect();
    let mut report = vec![
        "session_id (uuid): 1010 estimated rows, 4 of 4 partitions plus one for NULL, skew score 0.990".to_owned(),
        "rows  predicate".to_owned(),
        r#"  10  "session_id" IS NULL"#.to_owned(),
    ];
    report.extend(hashes.iter().map(|sql| format!(" 250  {sql}")));
    report.push("warning full_scans: each of the 4 hash predicates reads the whole table: no index on the column can serve them, so the read scans the table once per partition".to_owned());
    assert_eq!(result.report(), report.join("\n"));
    let summary =
        "skewsplit: 4 of 4 partitions plus one for NULL, with an estimated skew score of 0.990";
    assert!(result.to_pyspark().starts_with(&format!("# {summary}\n")));
    assert!(
        result
            .to_pyspark()
            .contains(&format!("\n    '{}',\n", hashes[3]))
    );
    assert!(result.to_scala().starts_with(&format!("// {summary}\n")));
    assert!(
        result
            .to_scala()
            .contains(&format!("\n  \"{}\"\n)", hashes[3].replace('"', "\\\"")))
    );
}

#[test]
fn errors() {
    let not_negative = "null_rows and non_null_rows must not be negative";
    let no_nul = "the column name must not be empty or contain U+0000";
    let type_no_nul = "the type must not be empty or contain U+0000";
    let cases: [(Value, ErrorCode, &str); 17] = [
        (
            json::parse(r#"["session_id"]"#).unwrap(),
            ErrorCode::InvalidInput,
            "the input must be a JSON object",
        ),
        (
            changed(&[("type", None), ("non_null_rows", None)]),
            ErrorCode::InvalidInput,
            "the input is missing type, non_null_rows",
        ),
        (
            changed(&[("mcv", Some("[]")), ("b", Some("1"))]),
            ErrorCode::InvalidInput,
            "the input has unknown fields: b, mcv",
        ),
        (
            changed(&[("column", Some("1"))]),
            ErrorCode::InvalidInput,
            "column must be a JSON string",
        ),
        (
            changed(&[("type", Some("null"))]),
            ErrorCode::InvalidInput,
            "type must be a JSON string",
        ),
        (
            changed(&[("partitions", Some(r#""4""#))]),
            ErrorCode::InvalidInput,
            "partitions must be a JSON integer",
        ),
        (
            changed(&[("null_rows", Some("true"))]),
            ErrorCode::InvalidInput,
            "null_rows must be a JSON integer",
        ),
        (
            changed(&[("non_null_rows", Some("2.5"))]),
            ErrorCode::InvalidInput,
            "non_null_rows must be a JSON integer",
        ),
        (
            changed(&[("column", Some(r#""""#))]),
            ErrorCode::InvalidColumn,
            no_nul,
        ),
        (
            changed(&[("column", Some(r#""a\u0000""#))]),
            ErrorCode::InvalidColumn,
            no_nul,
        ),
        (
            changed(&[("type", Some(r#""""#))]),
            ErrorCode::UnsupportedType,
            type_no_nul,
        ),
        (
            changed(&[("type", Some(r#""u\u0000""#))]),
            ErrorCode::UnsupportedType,
            type_no_nul,
        ),
        (
            changed(&[("partitions", Some("0"))]),
            ErrorCode::InvalidPartitions,
            "partitions must be 1 to 10000, not 0",
        ),
        (
            changed(&[("partitions", Some("10001"))]),
            ErrorCode::InvalidPartitions,
            "partitions must be 1 to 10000, not 10001",
        ),
        (
            changed(&[("null_rows", Some("-1"))]),
            ErrorCode::InvalidRows,
            not_negative,
        ),
        (
            changed(&[("non_null_rows", Some("-1"))]),
            ErrorCode::InvalidRows,
            not_negative,
        ),
        (
            changed(&[
                ("null_rows", Some("1")),
                ("non_null_rows", Some("9223372036854775807")),
            ]),
            ErrorCode::InvalidRows,
            "total_rows 9223372036854775808 is above 2^63 - 1",
        ),
    ];
    for (data, code, message) in cases {
        let error = hash_plan(&data).expect_err(message);
        assert_eq!((error.code(), error.message()), (code, message));
    }
}

/// JSON integers have any size, and Python's messages show them exactly.
#[test]
fn integers_beyond_i128_have_python_s_messages() {
    let huge = "1000000000000000000000000000000000000000";
    let cases = [
        (
            changed(&[("partitions", Some(huge))]),
            ErrorCode::InvalidPartitions,
            format!("partitions must be 1 to 10000, not {huge}"),
        ),
        (
            changed(&[("partitions", Some(&format!("-{huge}")))]),
            ErrorCode::InvalidPartitions,
            format!("partitions must be 1 to 10000, not -{huge}"),
        ),
        (
            changed(&[("null_rows", Some(&format!("-{huge}")))]),
            ErrorCode::InvalidRows,
            "null_rows and non_null_rows must not be negative".to_owned(),
        ),
        (
            changed(&[("null_rows", Some(huge)), ("non_null_rows", Some("1"))]),
            ErrorCode::InvalidRows,
            "total_rows 1000000000000000000000000000000000000001 is above 2^63 - 1".to_owned(),
        ),
        (
            changed(&[("null_rows", Some("1")), ("non_null_rows", Some(huge))]),
            ErrorCode::InvalidRows,
            "total_rows 1000000000000000000000000000000000000001 is above 2^63 - 1".to_owned(),
        ),
        (
            changed(&[
                ("null_rows", Some("170141183460469231731687303715884105727")),
                (
                    "non_null_rows",
                    Some("170141183460469231731687303715884105727"),
                ),
            ]),
            ErrorCode::InvalidRows,
            "total_rows 340282366920938463463374607431768211454 is above 2^63 - 1".to_owned(),
        ),
    ];
    for (data, code, message) in cases {
        let error = hash_plan(&data).expect_err(&message);
        assert_eq!((error.code(), error.message()), (code, message.as_str()));
    }
}
