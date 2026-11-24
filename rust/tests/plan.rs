//! The plan, its report (spec section 10) and its `PySpark` and Scala
//! snippets, with the cases of the Python package's
//! `python/tests/unit/test_plan.py`. `tests/fixtures.rs` checks the report and
//! the snippets of every fixture against Python's.

use skewsplit::json::{self, Value};
use skewsplit::plan::Plan;
use skewsplit::planner::plan;

const SECTION_1: &str = r#"{
    "column": "customer_id",
    "type": "int4",
    "partitions": 4,
    "null_rows": 100,
    "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
    "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400}
}"#;
const SUMMARY: &str = "customer_id (int4): 1060 estimated rows, 4 of 4 partitions plus one for NULL, skew score 1.887";
const DOMINANT: &str = "warning dominant_value: 42 holds 500 rows, at least one partition's share; it cannot be split further on this column";

fn planned(text: &str) -> Plan {
    plan(&json::parse(text).unwrap()).unwrap()
}

/// The plan of section 1 with another source.
fn with_source(source: &str) -> Plan {
    let mut result = planned(SECTION_1);
    result.source = json::parse(source).unwrap();
    result
}

fn report_lines(result: &Plan) -> Vec<String> {
    result.report().split('\n').map(str::to_owned).collect()
}

#[test]
fn predicates_are_the_sql_of_each_part() {
    let result = planned(SECTION_1);
    let sql: Vec<&str> = result.parts.iter().map(|part| part.sql.as_str()).collect();
    assert_eq!(result.predicates(), sql);
    assert_eq!(result.predicates()[2], r#""customer_id" = 42"#);
}

#[test]
fn the_report_of_section_1() {
    assert_eq!(
        report_lines(&planned(SECTION_1)),
        [
            SUMMARY,
            "rows  predicate",
            r#" 100  "customer_id" IS NULL"#,
            r#"  76  "customer_id" < 42"#,
            r#" 500  "customer_id" = 42"#,
            r#" 192  "customer_id" > 42 AND "customer_id" < 520"#,
            r#" 192  "customer_id" >= 520"#,
            DOMINANT,
        ]
    );
}

#[test]
fn the_report_aligns_wide_row_counts_and_shows_the_scale() {
    let result = planned(
        r#"{"column": "amount", "type": "numeric", "scale": 2, "partitions": 2, "null_rows": 7,
            "mcv": [], "histogram": {"bounds": ["1.00", "3.00"], "rows": 20000}}"#,
    );
    assert_eq!(
        report_lines(&result),
        [
            "amount (numeric, scale 2): 20007 estimated rows, 2 of 2 partitions plus one for NULL, skew score 1.000",
            " rows  predicate",
            r#"    7  "amount" IS NULL"#,
            r#"10000  "amount" < 2.00"#,
            r#"10000  "amount" >= 2.00"#,
        ]
    );
}

#[test]
fn the_report_of_a_plan_without_rows() {
    let result = planned(
        r#"{"column": "c", "type": "int4", "partitions": 3, "null_rows": 0, "mcv": [], "histogram": null}"#,
    );
    let lines = report_lines(&result);
    assert_eq!(
        lines[0],
        "c (int4): 0 estimated rows, 1 of 3 partitions plus one for NULL, no skew score"
    );
    assert_eq!(
        lines[1..4],
        [
            "rows  predicate",
            r#"   0  "c" IS NULL"#,
            r#"   0  "c" IS NOT NULL"#
        ]
    );
    let warnings: Vec<&str> = lines[4..]
        .iter()
        .map(|line| line.split(':').next().unwrap())
        .collect();
    assert_eq!(warnings, ["warning fewer_partitions", "warning no_rows"]);
}

#[test]
fn the_report_adds_the_well_formed_source_warnings() {
    let result = with_source(
        r#"{"statistics": "pg_stats", "warnings": [
            {"code": "stale_statistics", "modified_rows": 200, "rows": 1060, "message": "old"},
            "not a warning",
            {"code": 7, "message": "a code that is not text"},
            {"code": "no_message"}
        ]}"#,
    );
    let lines = report_lines(&result);
    assert_eq!(
        lines[lines.len() - 2..],
        [DOMINANT, "warning stale_statistics: old"]
    );
}

#[test]
fn a_source_without_a_list_of_warnings_adds_none() {
    for source in [
        "null",
        r#""pg_stats""#,
        r#"{"statistics": "pg_stats"}"#,
        r#"{"warnings": "none"}"#,
    ] {
        assert_eq!(
            with_source(source).report(),
            planned(SECTION_1).report(),
            "{source}"
        );
    }
}

#[test]
fn the_report_of_a_plan_from_a_sample_says_how_big_it_was() {
    let cases = [
        (r#"{"sample_rows": 30012}"#, "from a sample of 30012 rows"),
        (
            r#"{"sample_rows": 0, "planning_ms": 5}"#,
            "from a sample of 0 rows, planned in 5.000 ms",
        ),
        (
            r#"{"sample_rows": 7, "planning_ms": 12.3456}"#,
            "from a sample of 7 rows, planned in 12.346 ms",
        ),
        (
            r#"{"sample_rows": 7, "planning_ms": true}"#,
            "from a sample of 7 rows",
        ),
        (
            r#"{"sample_rows": 7, "planning_ms": "1"}"#,
            "from a sample of 7 rows",
        ),
        // As Python formats them: an integer through a float, ties to even,
        // and NaN and the infinities by name.
        (
            r#"{"sample_rows": 1, "planning_ms": 100000000000000000001}"#,
            "from a sample of 1 rows, planned in 100000000000000000000.000 ms",
        ),
        (
            r#"{"sample_rows": 1, "planning_ms": 0.0625}"#,
            "from a sample of 1 rows, planned in 0.062 ms",
        ),
        (
            r#"{"sample_rows": 1, "planning_ms": NaN}"#,
            "from a sample of 1 rows, planned in nan ms",
        ),
        (
            r#"{"sample_rows": 1, "planning_ms": Infinity}"#,
            "from a sample of 1 rows, planned in inf ms",
        ),
        (
            r#"{"sample_rows": 1, "planning_ms": -Infinity}"#,
            "from a sample of 1 rows, planned in -inf ms",
        ),
    ];
    for (source, line) in cases {
        let lines = report_lines(&with_source(source));
        assert_eq!(lines[1], line);
        assert!(lines[2].ends_with("  predicate"));
    }
}

#[test]
fn only_a_plan_from_a_sample_has_the_sample_line() {
    for source in [
        "null",
        "[]",
        r#"{"planning_ms": 5}"#,
        r#"{"sample_rows": "7"}"#,
        r#"{"sample_rows": true}"#,
    ] {
        assert!(
            report_lines(&with_source(source))[1].ends_with("  predicate"),
            "{source}"
        );
    }
}

#[test]
fn the_report_names_the_strategy_auto_chose_and_why() {
    let result = with_source(
        r#"{"auto": {"strategy": "sample", "reason": "statistics_stale", "message": "300 rows changed"},
            "sample_rows": 30012, "planning_ms": 5}"#,
    );
    assert_eq!(
        report_lines(&result)[1..4],
        [
            "strategy sample, chosen by auto: 300 rows changed",
            "from a sample of 30012 rows, planned in 5.000 ms",
            "rows  predicate",
        ]
    );
}

#[test]
fn only_a_choice_with_a_strategy_and_a_message_has_the_auto_line() {
    for auto in [
        "null",
        r#""sample""#,
        r#"{"strategy": "sample"}"#,
        r#"{"message": "why"}"#,
        r#"{"strategy": 1, "message": "why"}"#,
        r#"{"strategy": "sample", "message": null}"#,
    ] {
        let lines = report_lines(&with_source(&format!(r#"{{"auto": {auto}}}"#)));
        assert!(lines[1].ends_with("  predicate"), "{auto}");
    }
}

#[test]
fn to_json_is_the_plan_as_json_text() {
    let mut data = json::parse(SECTION_1).unwrap();
    let Value::Object(object) = &mut data else {
        unreachable!("the input is an object");
    };
    object.insert("column", Value::from("größe"));
    object.insert("source", json::parse(r#"{"table": "Straße"}"#).unwrap());
    let result = plan(&data).unwrap();
    let text = result.to_json(None).unwrap();
    assert_eq!(json::parse(&text).unwrap(), result.to_value());
    assert!(text.contains(r#""column": "größe""#));
    assert!(!text.contains('\n'));
    assert_eq!(
        result.to_json(Some(2)).unwrap(),
        result.to_value().to_json(Some(2)).unwrap()
    );
}

#[test]
fn to_json_refuses_a_source_json_cannot_hold() {
    let result = with_source(r#"{"n_distinct": NaN}"#);
    assert!(result.to_json(None).is_err());
}

// Hash predicates and their warning (spec section 16), as the hash fixtures
// write them; the hash planner itself is not ported yet.
#[test]
fn hash_predicates_and_full_scans_are_written_as_spec_section_16_says() {
    use skewsplit::plan::{Kind, PlanWarning, Predicate, WarningCode};
    let predicate = Predicate {
        kind: Kind::Hash(0),
        sql: r#"((pg_catalog.hashtextextended("session_id"::text, 0) % 4) + 4) % 4 = 0"#.to_owned(),
        estimated_rows: 250,
    };
    assert_eq!(
        predicate.to_value().to_json(None).unwrap(),
        r#"{"kind": "hash", "remainder": 0, "sql": "((pg_catalog.hashtextextended(\"session_id\"::text, 0) % 4) + 4) % 4 = 0", "estimated_rows": 250}"#
    );
    let warning = PlanWarning::new(WarningCode::FullScans, "each reads every row");
    assert_eq!(
        warning.to_value().to_json(None).unwrap(),
        r#"{"code": "full_scans", "message": "each reads every row"}"#
    );
}

const PYSPARK_SECTION_1: &str = r#"# skewsplit: 4 of 4 partitions plus one for NULL, with an estimated skew score of 1.887
# spark, url, table and properties are yours: the session, the JDBC URL, the table,
# and the connection properties such as the user and password.
predicates = [
    '"customer_id" IS NULL',
    '"customer_id" < 42',
    '"customer_id" = 42',
    '"customer_id" > 42 AND "customer_id" < 520',
    '"customer_id" >= 520',
]
df = spark.read.jdbc(url, table, predicates=predicates, properties=properties)"#;

const SCALA_SECTION_1: &str = r#"// skewsplit: 4 of 4 partitions plus one for NULL, with an estimated skew score of 1.887
// spark, url, table and properties are yours: the session, the JDBC URL, the table,
// and the connection properties such as the user and password.
val predicates = Array(
  "\"customer_id\" IS NULL",
  "\"customer_id\" < 42",
  "\"customer_id\" = 42",
  "\"customer_id\" > 42 AND \"customer_id\" < 520",
  "\"customer_id\" >= 520"
)
val df = spark.read.jdbc(url, table, predicates, properties)"#;

/// The plan of section 1 for another column name.
fn for_column(column: &str) -> Plan {
    let mut input = json::parse(SECTION_1).unwrap();
    let Value::Object(object) = &mut input else {
        unreachable!("the input is an object");
    };
    object.insert("column", Value::from(column));
    plan(&input).unwrap()
}

#[test]
fn the_snippets_of_section_1() {
    assert_eq!(planned(SECTION_1).to_pyspark(), PYSPARK_SECTION_1);
    assert_eq!(planned(SECTION_1).to_scala(), SCALA_SECTION_1);
}

#[test]
fn a_snippet_without_rows() {
    let result = planned(
        r#"{"column": "c", "type": "int4", "partitions": 3, "null_rows": 0, "mcv": [],
            "histogram": null}"#,
    );
    let summary = "skewsplit: 1 of 3 partitions plus one for NULL, with no skew score";
    assert_eq!(
        result.to_pyspark().split('\n').next(),
        Some(format!("# {summary}").as_str())
    );
    assert_eq!(
        result.to_scala().split('\n').next(),
        Some(format!("// {summary}").as_str())
    );
}

#[test]
fn the_comments_hold_nothing_from_the_column() {
    for column in ["new\nline", "*/ x /*", "carriage\rreturn"] {
        let result = for_column(column);
        // Only the predicates differ, so a line break in a name cannot end a comment.
        for (snippet, reference) in [
            (result.to_pyspark(), PYSPARK_SECTION_1),
            (result.to_scala(), SCALA_SECTION_1),
        ] {
            let lines: Vec<&str> = snippet.split('\n').collect();
            let expected: Vec<&str> = reference.split('\n').collect();
            assert_eq!(lines.len(), expected.len(), "{column:?}");
            assert_eq!(lines[..4], expected[..4], "{column:?}");
            assert_eq!(lines[lines.len() - 2..], expected[expected.len() - 2..]);
        }
    }
}

#[test]
fn a_predicate_is_a_python_string_literal_as_repr_writes_it() {
    for (column, literal) in [
        // Single quotes, unless the text has one and no double quote.
        ("it's", r#"'"it\'s" IS NULL'"#),
        ("a\\b", r#"'"a\\b" IS NULL'"#),
        ("tab\there", r#"'"tab\there" IS NULL'"#),
        // Characters that are not printable, as \x, \u or \U escapes.
        ("\u{1}\u{7f}\u{85}\u{a0}", r#"'"\x01\x7f\x85\xa0" IS NULL'"#),
        (
            "\u{2028}\u{202e}\u{feff}",
            r#"'"\u2028\u202e\ufeff" IS NULL'"#,
        ),
        ("\u{e0001}\u{10fffd}", r#"'"\U000e0001\U0010fffd" IS NULL'"#),
        // Printable characters as they are, the space and non-ASCII letters too.
        ("ü 😀 日", r#"'"ü 😀 日" IS NULL'"#),
    ] {
        let snippet = for_column(column).to_pyspark();
        assert_eq!(
            snippet.split('\n').nth(4),
            Some(format!("    {literal},").as_str())
        );
    }
}

#[test]
fn a_predicate_is_a_scala_string_literal_with_only_printable_characters() {
    for (column, literal) in [
        ("say \"hi\"", r#""\"say \"\"hi\"\"\" IS NULL""#),
        ("back\\slash", r#""\"back\\slash\" IS NULL""#),
        // A \u in the name is not an escape, because its backslash is doubled.
        ("\\u0022", r#""\"\\u0022\" IS NULL""#),
        (
            "a\nb\rc\td\u{8}e\u{c}f",
            r#""\"a\nb\rc\td\be\ff\" IS NULL""#,
        ),
        (
            "\u{1}\u{85}\u{a0}\u{2029}",
            r#""\"\u0001\u0085\u00a0\u2029\" IS NULL""#,
        ),
        // A character above U+FFFF is two UTF-16 units.
        ("\u{e0001}", r#""\"\udb40\udc01\" IS NULL""#),
        ("ü 😀 日 $x ${x}", r#""\"ü 😀 日 $x ${x}\" IS NULL""#),
    ] {
        let snippet = for_column(column).to_scala();
        assert_eq!(
            snippet.split('\n').nth(4),
            Some(format!("  {literal},").as_str())
        );
    }
}
