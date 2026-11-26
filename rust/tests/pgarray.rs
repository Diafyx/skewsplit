//! Reading PostgreSQL's array text (spec section 12), with the cases of the
//! Python package's `python/tests/unit/test_pgarray.py`. The shared cases, in
//! `spec/fixtures/pg-array/`, run in `tests/fixtures.rs`.

use skewsplit::codecs::codec_for;
use skewsplit::error::{ErrorCode, SpecError};
use skewsplit::pgarray::{parse_array, parse_elements, parse_values};

const VERTICAL_TAB: char = '\x0b';
const FORM_FEED: char = '\x0c';

fn error_of(text: &str) -> SpecError {
    parse_array(text).unwrap_err()
}

fn some(items: &[&str]) -> Vec<Option<String>> {
    items.iter().map(|item| Some((*item).to_owned())).collect()
}

#[test]
fn array_text_splits_into_its_elements() {
    // NULL in full-width letters: not ASCII, so not a NULL element.
    let wide_null: String = "NULL"
        .chars()
        .map(|letter| char::from_u32(u32::from(letter) + 0xFEE0).unwrap())
        .collect();
    let cases = [
        ("{}".to_owned(), Vec::new()),
        ("{a}".to_owned(), some(&["a"])),
        (
            r#"{"a b","c,d","{}","x\"y","\\"}"#.to_owned(),
            some(&["a b", "c,d", "{}", "x\"y", "\\"]),
        ),
        (
            "{\"line\nbreak\",\"tab\there\"}".to_owned(),
            some(&["line\nbreak", "tab\there"]),
        ),
        ("{\"a\\\nb\"}".to_owned(), some(&["a\nb"])),
        (r#"{""}"#.to_owned(), some(&[""])),
        ("{NULL,null,NuLl}".to_owned(), vec![None, None, None]),
        (r#"{"NULL"}"#.to_owned(), some(&["NULL"])),
        // Only ASCII letters spell NULL: these are ordinary text.
        (
            format!("{{NULLS,NUL,{wide_null}}}"),
            some(&["NULLS", "NUL", &wide_null]),
        ),
        ("{é,日本}".to_owned(), some(&["é", "日本"])),
        (
            "[-2147483648:-2147483647]={1,2}".to_owned(),
            some(&["1", "2"]),
        ),
        ("[2147483647:2147483647]={1}".to_owned(), some(&["1"])),
    ];
    for (text, items) in cases {
        assert_eq!(parse_array(&text).unwrap(), items, "{text:?}");
    }
}

#[test]
fn bad_array_text_is_an_invalid_array_that_says_why() {
    let cases = [
        ("[1:1]{1}", "'[1:1]{1}'", "an array starts with '{'"),
        // A bound starts with a digit, or with '-' and a digit. Without these,
        // only the property tests reach that check, when they happen to.
        ("[a:1]={1}", "'[a:1]={1}'", "an array starts with '{'"),
        ("[1:b]={1}", "'[1:b]={1}'", "an array starts with '{'"),
        ("[:1]={1}", "'[:1]={1}'", "an array starts with '{'"),
        ("[1:]={1}", "'[1:]={1}'", "an array starts with '{'"),
        ("[-:1]={1}", "'[-:1]={1}'", "an array starts with '{'"),
        (
            r#"{"a"b}"#,
            r#"'{"a"b}'"#,
            "elements are separated by ',' and end with '}'",
        ),
        ("{1}}", "'{1}}'", "nothing may follow the closing '}'"),
        (
            "[1:2]={1}",
            "'[1:2]={1}'",
            "the [L:U] prefix must match the number of elements",
        ),
        (
            "[-2147483649:-2147483649]={1}",
            "'[-2147483649:-2147483649]={1}'",
            "the [L:U] prefix must match",
        ),
        // A bound too long for an i64 is out of range too.
        (
            "[99999999999999999999:1]={1}",
            "'[99999999999999999999:1]={1}'",
            "the [L:U] prefix must match",
        ),
        ("{1,\t2}", r"'{1,\t2}'", "no valid element at offset 3"),
        // The offset counts characters, as Python's does, not bytes.
        ("{é,\t}", r"'{é,\t}'", "no valid element at offset 3"),
    ];
    for (text, shown, reason) in cases {
        let error = error_of(text);
        assert_eq!(error.code(), ErrorCode::InvalidArray, "{text:?}");
        let start = format!("{shown} is not array text: {reason}");
        assert!(error.message().starts_with(&start), "{}", error.message());
    }
}

#[test]
fn unquoted_elements_exclude_special_characters() {
    let specials = [
        '{',
        '}',
        '"',
        '\\',
        ' ',
        '\t',
        '\n',
        '\r',
        VERTICAL_TAB,
        FORM_FEED,
    ];
    for char in specials {
        assert_eq!(
            error_of(&format!("{{a{char}b}}")).code(),
            ErrorCode::InvalidArray,
            "{char:?}"
        );
        let escape = if matches!(char, '"' | '\\') { "\\" } else { "" };
        assert_eq!(
            parse_array(&format!("{{\"a{escape}{char}b\"}}")).unwrap(),
            some(&[&format!("a{char}b")]),
            "{char:?}"
        );
    }
}

#[test]
fn long_text_is_shortened_in_the_message() {
    let error = error_of(&format!("{{{}", "9".repeat(100)));
    let start = format!("'{{{}...' is not array text", "9".repeat(56));
    assert!(error.message().starts_with(&start), "{}", error.message());
}

#[test]
fn pg_stats_elements_are_never_null() {
    assert_eq!(parse_elements("{1,a}").unwrap(), ["1", "a"]);
    let error = parse_elements("{1,NULL}").unwrap_err();
    assert_eq!(error.code(), ErrorCode::InvalidArray);
    assert!(
        error.message().ends_with("pg_stats arrays never hold NULL"),
        "{}",
        error.message()
    );
}

#[test]
fn values_are_in_their_output_form() {
    let codec = codec_for("numeric", Some(3)).unwrap();
    assert_eq!(
        parse_values("{0.5,-2}", &codec).unwrap(),
        ["0.500", "-2.000"]
    );
}

#[test]
fn values_pass_the_codec_errors_on() {
    let codec = codec_for("int4", None).unwrap();
    for (text, code) in [
        (r#"{"1 "}"#, ErrorCode::InvalidValue),
        ("{2147483648}", ErrorCode::ValueOutOfRange),
        ("{1,NULL}", ErrorCode::InvalidArray),
    ] {
        assert_eq!(
            parse_values(text, &codec).unwrap_err().code(),
            code,
            "{text:?}"
        );
    }
}
