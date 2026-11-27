//! The value codecs (spec section 2), with the same cases as the Python
//! package's `python/tests/unit/test_codecs.py`.

use skewsplit::codecs::{Codec, INT64_MAX, INT64_MIN, TYPE_NAMES, codec_for};
use skewsplit::{ErrorCode, SpecError};

// Digits of another script, which a check for "numeric" characters would accept.
const FULLWIDTH_ONE: &str = "\u{ff11}";
const FULLWIDTH_TWO: &str = "\u{ff12}";

// A type and its scale, two ordinals or values, and so on, as in the Python cases.
type Limits = (
    &'static str,
    Option<i64>,
    (i128, i128),
    (&'static str, &'static str),
);
type Example<'a> = (&'a str, Option<i64>, &'a str, i128, Option<&'a str>);

fn codec(type_name: &str, scale: Option<i64>) -> Codec {
    codec_for(type_name, scale).expect("a supported type")
}

fn parse_error(type_name: &str, scale: Option<i64>, text: &str) -> SpecError {
    codec(type_name, scale)
        .parse(text)
        .expect_err("the text is refused")
}

// Spec section 2: the limits of every type, with their text forms.
#[test]
fn limits() {
    let cases: [Limits; 8] = [
        ("int2", None, (-32768, 32767), ("-32768", "32767")),
        (
            "int4",
            None,
            (-2_147_483_648, 2_147_483_647),
            ("-2147483648", "2147483647"),
        ),
        (
            "int8",
            None,
            (INT64_MIN, INT64_MAX),
            ("-9223372036854775808", "9223372036854775807"),
        ),
        (
            "numeric",
            Some(0),
            (INT64_MIN, INT64_MAX),
            ("-9223372036854775808", "9223372036854775807"),
        ),
        (
            "numeric",
            Some(2),
            (INT64_MIN, INT64_MAX),
            ("-92233720368547758.08", "92233720368547758.07"),
        ),
        (
            "date",
            None,
            (-719_162, 2_932_896),
            ("0001-01-01", "9999-12-31"),
        ),
        (
            "timestamp",
            None,
            (-62_135_596_800_000_000, 253_402_300_799_999_999),
            ("0001-01-01 00:00:00", "9999-12-31 23:59:59.999999"),
        ),
        (
            "timestamptz",
            None,
            (-62_135_596_800_000_000, 253_402_300_799_999_999),
            ("0001-01-01 00:00:00+00", "9999-12-31 23:59:59.999999+00"),
        ),
    ];
    for (type_name, scale, (low, high), (low_text, high_text)) in cases {
        let codec = codec(type_name, scale);
        assert_eq!((codec.min_ordinal(), codec.max_ordinal()), (low, high));
        assert_eq!(codec.text(low).unwrap(), low_text);
        assert_eq!(codec.text(high).unwrap(), high_text);
        assert_eq!(codec.parse(low_text).unwrap(), low);
        assert_eq!(codec.parse(high_text).unwrap(), high);
        for outside in [low - 1, high + 1] {
            let error = codec.text(outside).unwrap_err();
            assert_eq!(error.code(), ErrorCode::ValueOutOfRange);
            assert!(error.message().contains("outside"), "{error}");
            assert!(codec.literal(outside).is_err());
        }
    }
}

// The examples of spec section 2.
#[test]
fn parse_and_text() {
    let ones = format!("0.{}1", "0".repeat(999));
    let cases: [Example; 12] = [
        ("date", None, "2026-11-18", 20775, None),
        (
            "timestamptz",
            None,
            "2026-11-18 15:30:00.25+00",
            1_795_015_800_250_000,
            None,
        ),
        (
            "timestamp",
            None,
            "2026-11-18 15:30:00.250000",
            1_795_015_800_250_000,
            Some("2026-11-18 15:30:00.25"),
        ),
        (
            "timestamp",
            None,
            "1970-01-01 00:00:00.000000",
            0,
            Some("1970-01-01 00:00:00"),
        ),
        ("timestamp", None, "1969-12-31 23:59:59.999999", -1, None),
        ("numeric", Some(2), "12.5", 1250, Some("12.50")),
        ("numeric", Some(2), "-0.07", -7, None),
        ("numeric", Some(2), "0", 0, Some("0.00")),
        ("numeric", Some(0), "-12", -12, None),
        ("numeric", Some(1000), &ones, 1, None),
        ("int4", None, "-5", -5, None),
        ("int2", None, "0", 0, None),
    ];
    for (type_name, scale, text, ordinal, canonical) in cases {
        let codec = codec(type_name, scale);
        assert_eq!(codec.parse(text).unwrap(), ordinal, "{text}");
        assert_eq!(codec.text(ordinal).unwrap(), canonical.unwrap_or(text));
    }
}

// The rendering rules of spec section 5.
#[test]
fn literal() {
    let cases: [(&str, Option<i64>, &str, &str); 6] = [
        ("int4", None, "42", "42"),
        ("int8", None, "-5", "-5"),
        ("numeric", Some(2), "12.5", "12.50"),
        ("date", None, "2026-11-18", "'2026-11-18'::date"),
        (
            "timestamp",
            None,
            "2026-11-18 15:30:00",
            "'2026-11-18 15:30:00'::timestamp",
        ),
        (
            "timestamptz",
            None,
            "2026-11-18 15:30:00+00",
            "'2026-11-18 15:30:00+00'::timestamptz",
        ),
    ];
    for (type_name, scale, text, literal) in cases {
        let codec = codec(type_name, scale);
        assert_eq!(codec.literal(codec.parse(text).unwrap()).unwrap(), literal);
    }
}

#[test]
fn text_outside_the_form_is_invalid_value() {
    let fullwidth_one = FULLWIDTH_ONE.to_owned();
    let fullwidth_date = format!("{FULLWIDTH_TWO}026-11-18");
    let mut cases: Vec<(&str, Option<i64>, &str)> = Vec::new();
    for text in [
        "",
        "+1",
        "01",
        "1.0",
        "1e3",
        " 1",
        "1 ",
        "1\n",
        "1_000",
        &fullwidth_one,
        "--1",
    ] {
        cases.push(("int4", None, text));
    }
    for text in ["0.125", ".5", "1.", "+1", "01.5", "1e2", "1,5"] {
        cases.push(("numeric", Some(2), text));
    }
    cases.push(("numeric", Some(0), "1.0"));
    for text in [
        "2026-02-30",
        "2026-13-01",
        "0000-01-01",
        "2026-2-01",
        "20261118",
        "12026-01-01",
        "2026-11-18 00:00:00",
        &fullwidth_date,
        // ':' is the byte after '9', so without the digit check it would
        // count as 10 and make these real dates.
        "2026-0:-18",
        "202:-11-18",
        "2026-11-1:",
    ] {
        cases.push(("date", None, text));
    }
    for text in [
        "2026-11-18T15:30:00",
        "2026-11-18 15:30",
        "2026-11-18 24:00:00",
        "2026-11-18 15:60:00",
        "2026-11-18 15:30:60",
        "2026-11-18 15:30:00.",
        "2026-11-18 15:30:00.1234567",
        "2026-11-18 15:30:00+00",
        "2026-02-29 00:00:00",
        "2026-11-18 1::30:00",
        "2026-11-18 15:3::00",
        "2026-11-18 15:30:3:",
    ] {
        cases.push(("timestamp", None, text));
    }
    for text in [
        "2026-11-18 15:30:00",
        "2026-11-18 15:30:00+01",
        "2026-11-18 15:30:00+00:00",
        "2026-11-18 15:30:00Z",
        "2026-11-18 15:30:00.5 +00",
    ] {
        cases.push(("timestamptz", None, text));
    }
    for (type_name, scale, text) in cases {
        let error = parse_error(type_name, scale, text);
        assert_eq!(error.code(), ErrorCode::InvalidValue, "{text:?}");
        assert!(error.message().contains("expected"), "{error}");
    }
}

#[test]
fn invalid_values_explain_why() {
    let cases: [(&str, Option<i64>, &str, &str); 8] = [
        ("date", None, "infinity", "special values"),
        ("timestamptz", None, "-infinity", "special values"),
        ("numeric", Some(2), "NaN", "special values"),
        ("date", None, "0044-03-15 BC", "before year 1"),
        ("timestamp", None, "0001-12-31 23:00:00 BC", "before year 1"),
        ("int4", None, "-0", "without a minus sign"),
        ("numeric", Some(2), "-0.00", "without a minus sign"),
        ("numeric", Some(2), "-0", "without a minus sign"),
    ];
    for (type_name, scale, text, reason) in cases {
        let error = parse_error(type_name, scale, text);
        assert_eq!(error.code(), ErrorCode::InvalidValue);
        assert!(error.message().contains(reason), "{error}");
    }
}

#[test]
fn values_beyond_the_type_are_out_of_range() {
    let ones = "1".repeat(5000);
    let nines = format!("-{}", "9".repeat(30));
    let long_nines = "9".repeat(5000);
    let cases: [(&str, Option<i64>, &str); 12] = [
        ("int2", None, "32768"),
        ("int2", None, "-32769"),
        ("int4", None, "2147483648"),
        ("int8", None, "9223372036854775808"),
        ("int8", None, "-9223372036854775809"),
        ("int8", None, "10000000000000000000"),
        ("int8", None, &ones),
        ("int2", None, &nines),
        ("numeric", Some(2), "92233720368547758.08"),
        ("numeric", Some(2), "-92233720368547758.09"),
        ("numeric", Some(0), &long_nines),
        ("numeric", Some(1000), "1"),
    ];
    for (type_name, scale, text) in cases {
        let error = parse_error(type_name, scale, text);
        assert_eq!(
            error.code(),
            ErrorCode::ValueOutOfRange,
            "{type_name} {text}"
        );
    }
}

#[test]
fn messages_shorten_long_values() {
    let error = parse_error("int8", None, &"1".repeat(5000));
    let start = format!("'{}'... is outside the int8 range", "1".repeat(40));
    assert!(error.message().starts_with(&start), "{error}");
}

#[test]
fn codec_for_rejects_unsupported_types() {
    let cases: [(&str, Option<i64>, &str); 6] = [
        ("text", None, "type 'text' is not supported"),
        ("numeric", None, "needs its declared scale"),
        ("numeric", Some(-1), "outside 0 to 1000"),
        ("numeric", Some(1001), "outside 0 to 1000"),
        ("int4", Some(0), "only for numeric"),
        ("date", Some(2), "only for numeric"),
    ];
    for (type_name, scale, message) in cases {
        let error = codec_for(type_name, scale).expect_err("the type is refused");
        assert_eq!(error.code(), ErrorCode::UnsupportedType);
        assert!(error.message().contains(message), "{error}");
    }
}

#[test]
fn numeric_scale_limits_are_supported() {
    assert_eq!(codec("numeric", Some(0)).text(5).unwrap(), "5");
    let smallest = format!("-0.{}1", "0".repeat(999));
    assert_eq!(codec("numeric", Some(1000)).text(-1).unwrap(), smallest);
}

// What the Python tests get from the codec's attributes.
#[test]
fn each_codec_names_its_type_and_scale() {
    for type_name in TYPE_NAMES {
        let scale = (type_name == "numeric").then_some(3);
        let codec = codec(type_name, scale);
        assert_eq!(codec.type_name(), type_name);
        assert_eq!(
            codec.scale(),
            scale.map(|scale| u32::try_from(scale).unwrap())
        );
    }
}

// Messages quote values as Python's repr does for ordinary text.
#[test]
fn messages_quote_values_like_python() {
    let cases = [
        (
            "12x",
            "'12x' is not a valid int4 value: expected an integer such as 42 or -5",
        ),
        ("it's", r#""it's" is not a valid int4 value"#),
        (r#"it's "x""#, r#"'it\'s "x"' is not a valid int4 value"#),
        (r#"say "x""#, r#"'say "x"' is not a valid int4 value"#),
        ("a\\b", r"'a\\b' is not a valid int4 value"),
        ("1\n\r\t", r"'1\n\r\t' is not a valid int4 value"),
        ("\u{1b}", r"'\x1b' is not a valid int4 value"),
        ("é", "'é' is not a valid int4 value"),
    ];
    for (text, start) in cases {
        let error = parse_error("int4", None, text);
        assert!(error.message().starts_with(start), "{error}");
    }
    let error = codec_for("text", None).unwrap_err();
    assert_eq!(
        error.to_string(),
        "type 'text' is not supported; expected one of int2, int4, int8, date, timestamp, timestamptz, numeric"
    );
    let error = codec("int2", None).parse("40000").unwrap_err();
    assert_eq!(
        error.to_string(),
        "'40000' is outside the int2 range, -32768 to 32767"
    );
    let error = codec("int2", None).text(40000).unwrap_err();
    assert_eq!(
        error.to_string(),
        "ordinal 40000 is outside the int2 range, -32768 to 32767"
    );
}

// Each part of each text form: what one wrong character does.
#[test]
fn every_part_of_the_forms_is_checked() {
    let cases: [(&str, Option<i64>, &str); 14] = [
        ("numeric", Some(2), "1.2.3"),
        ("numeric", Some(2), "-"),
        ("numeric", Some(2), "-.5"),
        ("date", None, "2026/11/18"),
        ("date", None, "2026-1a-18"),
        ("date", None, "2026-00-18"),
        ("date", None, "2026-11-00"),
        ("date", None, "2026-11-31"),
        ("timestamp", None, "2026-11-18 15:30:00.12a"),
        ("timestamp", None, "2026-11-18 1a:30:00"),
        ("timestamp", None, "2026-11-18 15-30-00"),
        ("timestamp", None, "2026-11-31 15:30:00"),
        ("timestamp", None, "2026-11-18 15:30:00 "),
        ("timestamptz", None, "+00"),
    ];
    for (type_name, scale, text) in cases {
        let error = parse_error(type_name, scale, text);
        assert_eq!(error.code(), ErrorCode::InvalidValue, "{text:?}");
    }
}

// The leap years of the proleptic Gregorian calendar, which a calendar date
// must respect.
#[test]
fn leap_days() {
    let date = codec("date", None);
    for year in ["2024", "2000", "1600", "0004"] {
        assert!(date.parse(&format!("{year}-02-29")).is_ok(), "{year}");
    }
    for year in ["2026", "1900", "2100", "0001"] {
        assert!(date.parse(&format!("{year}-02-29")).is_err(), "{year}");
    }
    // February 29 of a year divisible by 400 is the last day of a 400-year
    // era of days_from_civil, where civil_from_days needs every term.
    let timestamp = codec("timestamp", None);
    for year in ["2000", "1600", "0400"] {
        let day = format!("{year}-02-29");
        assert_eq!(date.text(date.parse(&day).unwrap()).unwrap(), day);
        let time = format!("{day} 23:59:59");
        assert_eq!(
            timestamp.text(timestamp.parse(&time).unwrap()).unwrap(),
            time
        );
    }
    // Every day of a leap year round-trips, and the days are consecutive.
    let first = date.parse("2024-01-01").unwrap();
    let last = date.parse("2024-12-31").unwrap();
    assert_eq!(last - first, 365);
    for ordinal in first..=last {
        assert_eq!(date.parse(&date.text(ordinal).unwrap()).unwrap(), ordinal);
    }
}
