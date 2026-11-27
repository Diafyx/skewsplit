//! JSON as Python's `json` module reads and writes it. Every expected text
//! here is what Python 3.15 gives for the same input.

use std::path::{Path, PathBuf};

use skewsplit::json::{self, Int, MAX_DEPTH, Object, Value, float_repr};

fn parse(text: &str) -> Value {
    json::parse(text).expect("valid JSON")
}

fn error(text: &str) -> String {
    json::parse(text).expect_err("not JSON").to_string()
}

#[test]
fn values_of_every_kind() {
    let value = parse(
        r#"{"n": null, "t": true, "f": false, "i": -12, "x": 1.5, "s": "a", "l": [], "o": {}}"#,
    );
    let object = value.as_object().unwrap();
    assert_eq!(object.len(), 8);
    assert!(value.get("n").unwrap().is_null());
    assert_eq!(value.get("t"), Some(&Value::Bool(true)));
    assert_eq!(value.get("f"), Some(&Value::Bool(false)));
    assert_eq!(value.get("i").and_then(Value::as_i128), Some(-12));
    assert_eq!(value.get("x"), Some(&Value::Float(1.5)));
    assert_eq!(value.get("s").and_then(Value::as_str), Some("a"));
    assert_eq!(value.get("l").and_then(Value::as_array), Some(&[][..]));
    assert!(
        value
            .get("o")
            .and_then(Value::as_object)
            .unwrap()
            .is_empty()
    );
    assert_eq!(value.get("missing"), None);
    assert_eq!(
        value.to_json(None).unwrap(),
        r#"{"n": null, "t": true, "f": false, "i": -12, "x": 1.5, "s": "a", "l": [], "o": {}}"#
    );
}

#[test]
fn accessors_of_the_wrong_kind_give_none() {
    let value = parse("[1]");
    assert_eq!(value.get("a"), None);
    assert_eq!(value.as_str(), None);
    assert_eq!(value.as_int(), None);
    assert_eq!(value.as_i128(), None);
    assert_eq!(value.as_object(), None);
    assert!(!value.is_null());
    assert_eq!(parse("{}").as_array(), None);
}

#[test]
fn integers_keep_every_digit_and_stay_apart_from_floats() {
    let big = "-123456789012345678901234567890123456789012345678901234567890";
    let value = parse(big);
    let int = value.as_int().unwrap();
    assert_eq!(int.to_string(), big);
    assert_eq!(int.to_i128(), None);
    assert_eq!(int.saturating_i128(), i128::MIN);
    assert!(int.is_negative());
    let huge = parse("99999999999999999999999999999999999999999");
    assert_eq!(huge.as_int().unwrap().saturating_i128(), i128::MAX);
    assert_eq!(parse("1"), Value::from(1));
    assert_eq!(parse("1.0"), Value::Float(1.0));
    assert_eq!(parse("1e2"), Value::Float(100.0));
    assert_eq!(parse("1E+2"), Value::Float(100.0));
    assert_eq!(parse("-0"), Value::from(0));
    assert_eq!(parse("-0").to_json(None).unwrap(), "0");
    assert_eq!(parse("-0.0").to_json(None).unwrap(), "-0.0");
    assert_eq!(parse("1e400"), Value::Float(f64::INFINITY));
    assert_eq!(Int::from(42).to_string(), "42");
}

#[test]
fn python_reads_nan_and_the_infinities_but_does_not_write_them() {
    let value = parse("[NaN, Infinity, -Infinity]");
    let items = value.as_array().unwrap();
    assert!(matches!(items[0], Value::Float(x) if x.is_nan()));
    assert_eq!(items[1], Value::Float(f64::INFINITY));
    assert_eq!(items[2], Value::Float(f64::NEG_INFINITY));
    for (value, name) in [
        (f64::NAN, "nan"),
        (f64::INFINITY, "inf"),
        (f64::NEG_INFINITY, "-inf"),
    ] {
        let error = Value::Array(vec![Value::Float(value)])
            .to_json(None)
            .unwrap_err();
        assert_eq!(
            error.message(),
            format!("Out of range float values are not JSON compliant: {name}")
        );
    }
    let mut object = Object::new();
    object.insert("x", Value::Float(f64::NAN));
    assert!(Value::Object(object).to_json(Some(2)).is_err());
}

#[test]
fn a_repeated_key_keeps_its_place_and_its_last_value() {
    let value = parse(r#"{"a": 1, "b": 2, "a": 3}"#);
    assert_eq!(value.to_json(None).unwrap(), r#"{"a": 3, "b": 2}"#);
}

#[test]
fn strings_and_their_escapes() {
    let value = parse(r#""q\" b\\ s\/ \b\f\n\r\t \u00e9 \ud83d\ude00 é""#);
    assert_eq!(value.as_str(), Some("q\" b\\ s/ \u{8}\u{c}\n\r\t é 😀 é"));
    assert_eq!(parse(r#""\u0041\u00E9""#).as_str(), Some("Aé"));
    assert_eq!(parse("\"\u{7f}\"").as_str(), Some("\u{7f}"));
}

#[test]
fn python_writes_these_texts() {
    let value = Value::Object({
        let mut object = Object::new();
        object.insert(
            "a",
            Value::Array(vec![
                Value::from(1),
                Value::Array(vec![Value::from(2), Value::from(3)]),
                Value::Object(Object::new()),
                Value::Array(vec![]),
            ]),
        );
        let mut b = Object::new();
        b.insert("c", Value::Null);
        b.insert("d", Value::Bool(true));
        object.insert("b", Value::Object(b));
        object.insert("e", Value::from("x\"y\\z\n\t\u{1}\u{7f}é😀"));
        object.insert("f", Value::Float(-0.0));
        object.insert("g", parse("1000000000000000000000000000000"));
        object
    });
    assert_eq!(
        value.to_json(None).unwrap(),
        "{\"a\": [1, [2, 3], {}, []], \"b\": {\"c\": null, \"d\": true}, \"e\": \"x\\\"y\\\\z\\n\\t\\u0001\u{7f}é😀\", \"f\": -0.0, \"g\": 1000000000000000000000000000000}"
    );
    assert_eq!(
        value.to_json(Some(2)).unwrap(),
        "{\n  \"a\": [\n    1,\n    [\n      2,\n      3\n    ],\n    {},\n    []\n  ],\n  \"b\": {\n    \"c\": null,\n    \"d\": true\n  },\n  \"e\": \"x\\\"y\\\\z\\n\\t\\u0001\u{7f}é😀\",\n  \"f\": -0.0,\n  \"g\": 1000000000000000000000000000000\n}"
    );
    assert_eq!(
        value.to_json(Some(0)).unwrap(),
        "{\n\"a\": [\n1,\n[\n2,\n3\n],\n{},\n[]\n],\n\"b\": {\n\"c\": null,\n\"d\": true\n},\n\"e\": \"x\\\"y\\\\z\\n\\t\\u0001\u{7f}é😀\",\n\"f\": -0.0,\n\"g\": 1000000000000000000000000000000\n}"
    );
    assert_eq!(
        Value::from(String::from("\u{8}\u{c}\r"))
            .to_json(None)
            .unwrap(),
        r#""\b\f\r""#
    );
}

#[test]
fn floats_as_python_repr_writes_them() {
    let cases = [
        (0.0, "0.0"),
        (-0.0, "-0.0"),
        (0.1, "0.1"),
        (0.15, "0.15"),
        (1.5, "1.5"),
        (100.0, "100.0"),
        (1e16, "1e+16"),
        (1e15, "1000000000000000.0"),
        (1e-4, "0.0001"),
        (1e-5, "1e-05"),
        (5e-324, "5e-324"),
        (1.797_693_134_862_315_7e308, "1.7976931348623157e+308"),
        (123.25, "123.25"),
        (0.0015, "0.0015"),
        (1e22, "1e+22"),
        (-2.5e-10, "-2.5e-10"),
        // Two 16-digit texts read back as this float: Python writes the nearer.
        (983_300_536_950_290.2, "983300536950290.2"),
        (1_234_567_890_123_456.7, "1234567890123456.8"),
        // Next to a power of two, where the nearest 16 digits do not read back.
        (7.120_236_347_223_045e-307, "7.120236347223045e-307"),
    ];
    for (float, text) in cases {
        assert_eq!(float_repr(float).unwrap(), text, "{float:e}");
    }
}

#[test]
fn errors_say_what_and_where_as_python_does() {
    let cases = [
        ("[1,]", "Expecting value: line 1 column 4 (char 3)"),
        (
            r#"{"a":1,}"#,
            "Expecting property name enclosed in double quotes: line 1 column 8 (char 7)",
        ),
        (
            r#"{"a" 1}"#,
            "Expecting ':' delimiter: line 1 column 6 (char 5)",
        ),
        (
            "{1:2}",
            "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
        ),
        ("[1 2]", "Expecting ',' delimiter: line 1 column 4 (char 3)"),
        (
            r#"{"a":1 "b":2}"#,
            "Expecting ',' delimiter: line 1 column 8 (char 7)",
        ),
        (
            r#""abc"#,
            "Unterminated string starting at: line 1 column 1 (char 0)",
        ),
        (
            r#""a\"#,
            "Unterminated string starting at: line 1 column 1 (char 0)",
        ),
        (
            "\"a\u{1}\"",
            "Invalid control character at: line 1 column 3 (char 2)",
        ),
        (r#""\x""#, "Invalid \\escape: line 1 column 2 (char 1)"),
        (
            r#""\u12""#,
            "Invalid \\uXXXX escape: line 1 column 3 (char 2)",
        ),
        ("1 2", "Extra data: line 1 column 3 (char 2)"),
        (
            "\u{feff}1",
            "Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)",
        ),
        ("", "Expecting value: line 1 column 1 (char 0)"),
        ("  \n  x", "Expecting value: line 2 column 3 (char 5)"),
        ("[\n\"é\", @]", "Expecting value: line 2 column 6 (char 7)"),
        ("-", "Expecting value: line 1 column 1 (char 0)"),
        ("nul", "Expecting value: line 1 column 1 (char 0)"),
    ];
    for (text, message) in cases {
        assert_eq!(error(text), message, "{text:?}");
    }
}

#[test]
fn numbers_end_where_python_ends_them() {
    for text in ["01", "1.", ".5", "+1", "1e", "1e+", "--1", "1.e5"] {
        assert!(json::parse(text).is_err(), "{text}");
    }
    assert_eq!(
        parse("[1e5, 2.5E-3]").to_json(None).unwrap(),
        "[100000.0, 0.0025]"
    );
}

// A Rust string cannot hold a lone surrogate, which Python reads.
#[test]
fn lone_surrogates_are_refused() {
    for text in [
        r#""\ud800""#,
        r#""\udc00""#,
        r#""\ud800\u0041""#,
        r#""\ud800x""#,
    ] {
        assert!(
            error(text).starts_with("Lone surrogate in \\uXXXX escape"),
            "{text}"
        );
    }
    assert!(error(r#""\ud800\u12""#).starts_with("Invalid \\uXXXX escape"));
}

#[test]
fn nesting_deeper_than_the_limit_is_refused() {
    let deep = |depth: usize| format!("{}{}", "[".repeat(depth), "]".repeat(depth));
    assert!(json::parse(&deep(MAX_DEPTH)).is_ok());
    assert!(error(&deep(MAX_DEPTH + 1)).starts_with("Too deeply nested"));
    let objects = format!(
        "{}1{}",
        r#"{"a":"#.repeat(MAX_DEPTH + 1),
        "}".repeat(MAX_DEPTH + 1)
    );
    assert!(error(&objects).starts_with("Too deeply nested"));
}

#[test]
fn an_object_keeps_its_order_and_can_change() {
    let mut object = Object::new();
    assert!(object.is_empty());
    object.insert("b", Value::from(1));
    assert!(!object.is_empty());
    object.insert("a", Value::from(2));
    object.insert("b", Value::from(3));
    assert_eq!(object.keys().collect::<Vec<_>>(), ["b", "a"]);
    assert!(object.contains_key("a"));
    assert_eq!(object.remove("a"), Some(Value::from(2)));
    assert_eq!(object.remove("a"), None);
    let entries: Vec<(&str, &Value)> = object.iter().collect();
    assert_eq!(entries, [("b", &Value::from(3))]);
    assert_eq!(Value::Object(object).to_json(None).unwrap(), r#"{"b": 3}"#);
}

#[test]
fn errors_are_errors() {
    let error: Box<dyn std::error::Error> = Box::new(json::parse("x").unwrap_err());
    assert_eq!(
        error.to_string(),
        "Expecting value: line 1 column 1 (char 0)"
    );
}

fn fixtures(folder: &Path) -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(folder)
        .expect("the fixtures are readable")
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .collect();
    paths.sort();
    paths
}

fn spec_fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../spec/fixtures")
}

// The plan fixtures are written by json.dumps(indent=2), so writing what was
// read gives back every byte.
#[test]
fn every_plan_fixture_is_written_back_byte_for_byte() {
    let paths = fixtures(&spec_fixtures());
    assert!(paths.len() >= 40);
    for path in paths {
        let text = std::fs::read_to_string(&path).unwrap();
        let value = parse(&text);
        assert_eq!(
            value.to_json(Some(2)).unwrap() + "\n",
            text,
            "{}",
            path.display()
        );
    }
}

// Some of the other fixtures are laid out by hand, so their values must
// survive writing in both layouts.
#[test]
fn every_other_fixture_reads_back_the_same() {
    let mut count = 0;
    for folder in ["hash", "pg-array", "pg-stats", "sample"] {
        for path in fixtures(&spec_fixtures().join(folder)) {
            let value = parse(&std::fs::read_to_string(&path).unwrap());
            for indent in [None, Some(2)] {
                assert_eq!(
                    parse(&value.to_json(indent).unwrap()),
                    value,
                    "{}",
                    path.display()
                );
            }
            count += 1;
        }
    }
    assert!(count >= 140);
}

// Python escapes every control character up to U+001F, and no other.
#[test]
fn control_characters_are_escaped_up_to_u001f() {
    assert_eq!(
        Value::from("\u{1f}\u{0} \u{7f}").to_json(None).unwrap(),
        "\"\\u001f\\u0000 \u{7f}\""
    );
}

// Only space, tab, newline and carriage return separate tokens.
#[test]
fn other_whitespace_is_not_json() {
    let cases = [
        ("\u{b}1", "Expecting value: line 1 column 1 (char 0)"),
        ("\u{c}1", "Expecting value: line 1 column 1 (char 0)"),
        ("\u{a0}1", "Expecting value: line 1 column 1 (char 0)"),
        ("[1,\u{b}2]", "Expecting value: line 1 column 4 (char 3)"),
    ];
    for (text, message) in cases {
        assert_eq!(error(text), message, "{text:?}");
    }
}
