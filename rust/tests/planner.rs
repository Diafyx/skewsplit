//! The planner (spec sections 3 to 8), with the cases of the Python package's
//! `python/tests/unit/test_planner.py`.

use skewsplit::ErrorCode;
use skewsplit::json::{self, Object, Value};
use skewsplit::plan::{Kind, Plan, error_value};
use skewsplit::planner::plan;

/// An `int4` input with 4 partitions, these bounds and common values, and
/// each other field set to the JSON text given.
fn make(bounds: Option<&str>, rows: i128, mcv: &[(&str, i128)], fields: &[(&str, &str)]) -> Value {
    let mut data = Object::new();
    for (key, value) in [
        ("column", r#""c""#),
        ("type", r#""int4""#),
        ("partitions", "4"),
        ("null_rows", "0"),
    ] {
        data.insert(key, json::parse(value).unwrap());
    }
    let entries = mcv
        .iter()
        .map(|&(value, rows)| {
            json::parse(&format!(r#"{{"value": "{value}", "rows": {rows}}}"#)).unwrap()
        })
        .collect();
    data.insert("mcv", Value::Array(entries));
    let histogram = bounds.map_or(Value::Null, |bounds| {
        json::parse(&format!(r#"{{"bounds": {bounds}, "rows": {rows}}}"#)).unwrap()
    });
    data.insert("histogram", histogram);
    for &(key, value) in fields {
        data.insert(key, json::parse(value).unwrap());
    }
    Value::Object(data)
}

fn planned(data: &Value) -> Plan {
    plan(data).expect("a valid input")
}

/// The non-NULL predicates with their estimated rows.
fn table(result: &Plan) -> Vec<(String, i128)> {
    result.parts[1..]
        .iter()
        .map(|part| (part.sql.replace("\"c\" ", ""), part.estimated_rows))
        .collect()
}

fn rows(expected: &[(&str, i128)]) -> Vec<(String, i128)> {
    expected
        .iter()
        .map(|&(sql, rows)| (sql.to_owned(), rows))
        .collect()
}

fn codes(result: &Plan) -> Vec<&str> {
    result
        .warnings
        .iter()
        .map(|warning| warning.code.as_str())
        .collect()
}

const BOUNDS_1: &str = r#"["1", "250", "500", "750", "1000"]"#;
const WIDE: &str = r#"["1", "10", "20", "1000000"]"#;

fn section_1() -> Value {
    make(
        Some(BOUNDS_1),
        400,
        &[("7", 60), ("42", 500)],
        &[("column", r#""customer_id""#), ("null_rows", "100")],
    )
}

#[test]
fn the_example_of_section_10() {
    let expected = r#"{"spec_version": "1.1", "column": "customer_id", "type": "int4", "requested_partitions": 4, "planned_partitions": 4, "rows": {"total": 1060, "null": 100, "non_null": 960}, "predicates": [{"kind": "null", "sql": "\"customer_id\" IS NULL", "estimated_rows": 100}, {"kind": "range", "lower": null, "upper": {"op": "<", "value": "42"}, "sql": "\"customer_id\" < 42", "estimated_rows": 76}, {"kind": "value", "value": "42", "sql": "\"customer_id\" = 42", "estimated_rows": 500}, {"kind": "range", "lower": {"op": ">", "value": "42"}, "upper": {"op": "<", "value": "520"}, "sql": "\"customer_id\" > 42 AND \"customer_id\" < 520", "estimated_rows": 192}, {"kind": "range", "lower": {"op": ">=", "value": "520"}, "upper": null, "sql": "\"customer_id\" >= 520", "estimated_rows": 192}], "skew_score": 1.887, "warnings": [{"code": "dominant_value", "value": "42", "rows": 500, "message": "42 holds 500 rows, at least one partition's share; it cannot be split further on this column"}], "source": null}"#;
    assert_eq!(planned(&section_1()).to_json(None).unwrap(), expected);
}

// Section 4.1: dominant values.
#[test]
fn dominant_values() {
    // N, the common values, and the values that are dominant or not isolated.
    type Case<'a> = (i128, &'a [(&'a str, i128)], &'a [&'a str], &'a [&'a str]);
    let cases: [Case; 5] = [
        (4, &[("7", 60), ("42", 500)], &["42"], &[]),
        (2, &[("1", 700)], &[], &["1"]),
        (
            6,
            &[("7", 3000), ("42", 2500), ("99", 1700)],
            &["7", "42"],
            &["99"],
        ),
        // 300 * 4 is equal to R, so 10 is a candidate; 20 wins the tie with 30.
        (
            4,
            &[("10", 300), ("20", 400), ("30", 400)],
            &["20"],
            &["10", "30"],
        ),
        (3, &[("5", 1000)], &["5"], &[]),
    ];
    for (n, mcv, dominant, not_isolated) in cases {
        let total: i128 = mcv.iter().map(|&(_, rows)| rows).sum();
        let rest = if n == 4 && mcv.len() == 3 {
            1200 - total
        } else {
            400
        };
        let partitions = n.to_string();
        let result = planned(&make(
            Some(r#"["1000", "2000"]"#),
            rest,
            mcv,
            &[("partitions", &partitions)],
        ));
        let values = |code: &str| -> Vec<&str> {
            result
                .warnings
                .iter()
                .filter(|warning| warning.code.as_str() == code)
                .filter_map(|warning| warning.value.as_deref())
                .collect()
        };
        assert_eq!(values("dominant_value"), dominant);
        assert_eq!(values("dominant_value_not_isolated"), not_isolated);
        let isolated: Vec<&str> = result
            .parts
            .iter()
            .filter_map(|part| match &part.kind {
                Kind::Value(value) => Some(value.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(isolated, dominant);
    }
}

// Section 4.3: split points.
#[test]
fn nearest_candidate() {
    let result = planned(&make(Some(WIDE), 900, &[], &[]));
    assert_eq!(
        table(&result),
        rows(&[
            ("< 8", 233),
            (">= 8 AND < 15", 217),
            (">= 15 AND < 250015", 225),
            (">= 250015", 225),
        ])
    );
}

#[test]
fn targets_round_down() {
    // 3 rows over 0 to 3 for 2 ranges: the target is 3 / 2 = 1, not 2.
    let result = planned(&make(Some(r#"["0", "3"]"#), 3, &[], &[("partitions", "2")]));
    assert_eq!(table(&result), rows(&[("< 1", 1), (">= 1", 2)]));
}

#[test]
fn tie_between_candidates_takes_x_hi() {
    let data = make(
        Some(r#"["0", "4"]"#),
        4,
        &[("5", 4), ("9", 4)],
        &[("partitions", "2")],
    );
    assert_eq!(table(&planned(&data)), rows(&[("< 6", 8), (">= 6", 4)]));
}

#[test]
fn split_point_at_the_type_limit() {
    let mcv = [("0", 100), ("50", 30), ("32767", 30)];
    let data = make(
        Some(r#"["1", "100"]"#),
        20,
        &mcv,
        &[("type", r#""int2""#), ("partitions", "5")],
    );
    assert_eq!(
        table(&planned(&data)),
        rows(&[
            ("< 0", 0),
            ("= 0", 100),
            ("> 0 AND < 51", 40),
            (">= 51 AND < 32767", 10),
            (">= 32767", 30),
        ])
    );
}

// A split point at the type's largest value: x_hi is MAX itself, and x_lo is
// not a candidate, so x_hi must still be one.
#[test]
fn split_point_at_the_largest_value_of_the_type() {
    let data = make(
        Some(r#"["32762", "32767", "32767"]"#),
        2,
        &[],
        &[("type", r#""int2""#)],
    );
    let result = planned(&data);
    assert_eq!(table(&result), rows(&[("< 32767", 1), (">= 32767", 1)]));
    assert_eq!(codes(&result), ["fewer_partitions"]);
}

#[test]
fn dropped_split_point_gives_fewer_partitions() {
    let result = planned(&make(Some(r#"["1", "5", "5", "5", "100"]"#), 400, &[], &[]));
    assert_eq!(
        table(&result),
        rows(&[("< 5", 100), (">= 5 AND < 6", 201), (">= 6", 99)])
    );
    assert_eq!(
        result.warnings[0].to_value().get("requested"),
        Some(&Value::from(4))
    );
    assert_eq!(
        (result.planned_partitions(), codes(&result)),
        (3, vec!["fewer_partitions"])
    );
}

#[test]
fn x_lo_is_not_a_candidate_at_the_lower_limit() {
    // Above dominant value 1, the first ordinal, 2, already reaches the target.
    let data = make(
        Some(r#"["0", "2"]"#),
        1000,
        &[("1", 2000)],
        &[("partitions", "5")],
    );
    assert_eq!(
        table(&planned(&data)),
        rows(&[("< 1", 500), ("= 1", 2000), ("> 1", 500)])
    );
}

// Section 4.4: fewer partitions.
#[test]
fn fewer_distinct_values_than_partitions() {
    let data = make(
        None,
        0,
        &[("1", 100), ("2", 100), ("3", 100)],
        &[("type", r#""int2""#), ("partitions", "8")],
    );
    let result = planned(&data);
    assert_eq!(
        table(&result),
        rows(&[
            ("< 1", 0),
            ("= 1", 100),
            ("> 1 AND < 2", 0),
            ("= 2", 100),
            ("> 2 AND < 3", 0),
            ("= 3", 100),
            ("> 3", 0),
        ])
    );
    assert_eq!(codes(&result).last(), Some(&"fewer_partitions"));
}

// Section 5: predicates and rendering.
#[test]
fn one_partition_is_is_not_null() {
    let data = make(
        Some(r#"["1", "500", "1000"]"#),
        1000,
        &[],
        &[("null_rows", "5"), ("partitions", "1")],
    );
    let result = planned(&data);
    assert_eq!(table(&result), rows(&[("IS NOT NULL", 1000)]));
    assert_eq!((result.skew_score(), codes(&result)), (Some(0.995), vec![]));
}

#[test]
fn column_is_quoted() {
    for (column, quoted) in [
        (r#""customer_id""#, r#""customer_id""#),
        (r#""a\"b""#, r#""a""b""#),
        (r#""Order Id""#, r#""Order Id""#),
    ] {
        let data = make(
            Some(r#"["1", "3"]"#),
            2,
            &[],
            &[("column", column), ("partitions", "2")],
        );
        assert_eq!(
            planned(&data).predicates(),
            [
                format!("{quoted} IS NULL"),
                format!("{quoted} < 2"),
                format!("{quoted} >= 2")
            ]
        );
    }
}

#[test]
fn dates_and_timestamps_are_quoted_with_a_cast() {
    let cases = [
        (
            r#""date""#,
            r#"["2026-01-01", "2026-01-03"]"#,
            "\"c\" < '2026-01-02'::date",
        ),
        (
            r#""timestamp""#,
            r#"["2026-11-18 00:00:00", "2026-11-18 00:00:01"]"#,
            "\"c\" < '2026-11-18 00:00:00.5'::timestamp",
        ),
        (
            r#""timestamptz""#,
            r#"["2026-11-18 00:00:00+00", "2026-11-18 02:00:00+00"]"#,
            "\"c\" < '2026-11-18 01:00:00+00'::timestamptz",
        ),
    ];
    for (type_name, bounds, sql) in cases {
        let data = make(
            Some(bounds),
            2,
            &[],
            &[("type", type_name), ("partitions", "2")],
        );
        assert_eq!(planned(&data).parts[1].sql, sql);
    }
}

#[test]
fn numeric_keeps_its_scale() {
    let data = make(
        Some(r#"["0", "1"]"#),
        2,
        &[],
        &[
            ("type", r#""numeric""#),
            ("scale", "2"),
            ("partitions", "2"),
        ],
    );
    let result = planned(&data);
    let out = result.to_value();
    assert_eq!(out.get("type").and_then(Value::as_str), Some("numeric"));
    assert_eq!(out.get("scale").and_then(Value::as_i128), Some(2));
    assert_eq!(table(&result), rows(&[("< 0.50", 1), (">= 0.50", 1)]));
    assert_eq!(planned(&section_1()).to_value().get("scale"), None);
}

// Section 7: skew score.
#[test]
fn skew_score_of_an_empty_table_is_null() {
    let result = planned(&make(None, 0, &[], &[]));
    assert_eq!(table(&result), rows(&[("IS NOT NULL", 0)]));
    assert_eq!((result.skew_milli, result.skew_score()), (None, None));
    assert_eq!(codes(&result), ["fewer_partitions", "no_rows"]);
    assert!(result.to_value().get("skew_score").unwrap().is_null());
}

#[test]
fn skew_score_rounds_half_up() {
    // 233 * 4 / 900 = 1.03555..., and 8 * 2 / 12 = 1.3333...
    assert_eq!(
        planned(&make(Some(WIDE), 900, &[], &[])).skew_score(),
        Some(1.036)
    );
    let data = make(
        Some(r#"["0", "4"]"#),
        4,
        &[("5", 4), ("9", 4)],
        &[("partitions", "2")],
    );
    assert_eq!(planned(&data).skew_score(), Some(1.333));
}

// Section 8: warnings.
#[test]
fn nulls_exceed_share() {
    let bounds = r#"["2026-01-01", "2026-04-01", "2026-07-01", "2026-10-01"]"#;
    let data = make(
        Some(bounds),
        300,
        &[],
        &[
            ("type", r#""date""#),
            ("null_rows", "700"),
            ("partitions", "3"),
        ],
    );
    let result = planned(&data);
    let warnings: Vec<String> = result
        .warnings
        .iter()
        .map(|warning| warning.to_value().to_json(None).unwrap())
        .collect();
    assert_eq!(
        warnings,
        [
            r#"{"code": "nulls_exceed_share", "rows": 700, "message": "700 NULL rows are more than one partition's share"}"#
        ]
    );
    assert_eq!(result.skew_score(), Some(2.1));
}

#[test]
fn all_null() {
    let result = planned(&make(
        None,
        0,
        &[],
        &[("null_rows", "10"), ("partitions", "3")],
    ));
    assert_eq!(
        result.predicates(),
        [r#""c" IS NULL"#, r#""c" IS NOT NULL"#]
    );
    assert_eq!(codes(&result), ["fewer_partitions", "nulls_exceed_share"]);
    assert_eq!(result.skew_score(), Some(3.0));
}

#[test]
fn source_is_copied() {
    let source = r#"{"strategy": "pg_stats", "analyzed": "2026-11-18 00:00:00+00"}"#;
    let result = planned(&make(Some(r#"["1", "3"]"#), 2, &[], &[("source", source)]));
    assert_eq!(
        result.to_value().get("source"),
        Some(&json::parse(source).unwrap())
    );
}

#[test]
fn invalid_input_is_a_spec_error() {
    let error = plan(&make(None, 0, &[], &[("partitions", "0")])).unwrap_err();
    assert_eq!(error.code(), ErrorCode::InvalidPartitions);
    assert_eq!(
        error_value(&error).to_json(None).unwrap(),
        format!(
            r#"{{"spec_version": "1.1", "error": {{"code": "invalid_partitions", "message": "{}"}}}}"#,
            error.message()
        )
    );
}

// The warnings of a value too common to split but not isolated, and of a plan
// with fewer partitions, carry the fields spec section 8 gives them.
#[test]
fn every_warning_has_its_fields_and_message() {
    let data = make(
        Some(r#"["1000", "2000"]"#),
        400,
        &[("1", 700)],
        &[("partitions", "2")],
    );
    let warnings: Vec<String> = planned(&data)
        .warnings
        .iter()
        .map(|warning| warning.to_value().to_json(None).unwrap())
        .collect();
    assert_eq!(
        warnings,
        [
            r#"{"code": "dominant_value_not_isolated", "value": "1", "rows": 700, "message": "1 holds 700 rows, at least one partition's share, but 2 partitions are too few to give it its own predicate"}"#
        ]
    );
    let fewer = planned(&make(Some(r#"["1", "5", "5", "5", "100"]"#), 400, &[], &[]));
    assert_eq!(
        fewer.warnings[0].to_value().to_json(None).unwrap(),
        r#"{"code": "fewer_partitions", "requested": 4, "planned": 3, "message": "planned 3 of the 4 requested partitions: the column has too few distinct values, or too many rows at single values"}"#
    );
    let empty = planned(&make(None, 0, &[], &[]));
    assert_eq!(
        empty.warnings[1].to_value().to_json(None).unwrap(),
        r#"{"code": "no_rows", "message": "the statistics show no rows; the plan still reads rows added later"}"#
    );
}
