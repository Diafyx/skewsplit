//! Fuzz the array text reader of spec section 12 (`skewsplit::pgarray`) with
//! the same checks as the property tests, on any text:
//!
//! - it reads as elements, or fails with `invalid_array`;
//! - the elements read back when printed as PostgreSQL prints them;
//! - for every type, it reads as values in their output form, or fails with
//!   `invalid_array`, `invalid_value` or `value_out_of_range`.
//!
//! Any other failure, or a panic, is a crash, which libFuzzer saves.

#![no_main]

use libfuzzer_sys::fuzz_target;
use skewsplit::codecs::codec_for;
use skewsplit::error::ErrorCode;
use skewsplit::pgarray::{parse_array, parse_values};

const TYPES: [(&str, Option<i64>); 8] = [
    ("int2", None),
    ("int4", None),
    ("int8", None),
    ("numeric", Some(2)),
    ("numeric", Some(0)),
    ("date", None),
    ("timestamp", None),
    ("timestamptz", None),
];
/// The characters PostgreSQL quotes an element for.
const SPECIAL: [char; 11] = [
    '{', '}', ',', '"', '\\', ' ', '\t', '\n', '\r', '\x0b', '\x0c',
];

/// How PostgreSQL prints an element (`array_out` in
/// `src/backend/utils/adt/arrayfuncs.c`), as in `tests/properties.rs`.
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

fuzz_target!(|data: &[u8]| {
    let Ok(text) = std::str::from_utf8(data) else {
        return;
    };
    match parse_array(text) {
        Ok(items) => {
            let printed: Vec<String> = items.iter().map(|item| quote(item.as_deref())).collect();
            let printed = format!("{{{}}}", printed.join(","));
            assert_eq!(parse_array(&printed).unwrap(), items, "{printed:?}");
        }
        Err(error) => assert_eq!(error.code(), ErrorCode::InvalidArray, "{error:?}"),
    }
    for (type_name, scale) in TYPES {
        let codec = codec_for(type_name, scale).unwrap();
        match parse_values(text, &codec) {
            Ok(values) => {
                for value in values {
                    let again = codec.text(codec.parse(&value).unwrap()).unwrap();
                    assert_eq!(again, value, "{type_name}");
                }
            }
            Err(error) => assert!(
                matches!(
                    error.code(),
                    ErrorCode::InvalidArray | ErrorCode::InvalidValue | ErrorCode::ValueOutOfRange
                ),
                "{error:?}"
            ),
        }
    }
});
