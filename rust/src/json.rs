//! JSON values, read and written as Python's `json` module does.
//!
//! The planner reads its input as JSON and writes plans as JSON, and every
//! implementation must write the same bytes (spec section 10). This module
//! follows `json.loads` and `json.dumps(value, indent=..., ensure_ascii=False,
//! allow_nan=False)`:
//!
//! - integers keep every digit, however large, and stay apart from floats:
//!   `1` is an integer and `1.0` a float;
//! - floats are written as Python's `repr` writes them, such as `0.1`,
//!   `1e-05` and `1e+16`;
//! - an object keeps the order of its keys, and a repeated key keeps its
//!   first place and its last value;
//! - `NaN`, `Infinity` and `-Infinity` are read, but writing them is an error.
//!
//! Two differences: text with a lone UTF-16 surrogate (such as `"\ud800"`)
//! is refused, because a Rust string cannot hold one, and so is nesting more
//! than 512 levels deep, where Python's recursion limit stops it too.
//!
//! ```
//! use skewsplit::json::{self, Value};
//!
//! let value = json::parse(r#"{"rows": 12, "share": 0.25, "tags": ["a"]}"#)?;
//! assert_eq!(value.get("rows").and_then(Value::as_i128), Some(12));
//! assert_eq!(value.to_json(None)?, r#"{"rows": 12, "share": 0.25, "tags": ["a"]}"#);
//! # Ok::<(), json::JsonError>(())
//! ```

use std::collections::HashMap;
use std::fmt::{self, Write};

/// How deep arrays and objects may nest.
pub const MAX_DEPTH: usize = 512;

/// A JSON value.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool(bool),
    /// An integer, with every digit.
    Int(Int),
    /// A number with a fraction or an exponent, or `NaN` or an infinity.
    Float(f64),
    /// A string.
    String(String),
    /// An array.
    Array(Vec<Value>),
    /// An object, in key order.
    Object(Object),
}

/// A JSON integer of any size, kept as its canonical decimal digits.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Int {
    // `0`, or an optional `-` and digits without a leading zero.
    text: String,
}

/// A JSON object: its keys and values in the order they were added.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Object {
    entries: Vec<(String, Value)>,
}

/// JSON text that cannot be read, or a value that JSON has no form for.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JsonError {
    message: String,
}

impl Int {
    /// The integer if it fits in an `i128`.
    #[must_use]
    pub fn to_i128(&self) -> Option<i128> {
        self.text.parse().ok()
    }

    /// The integer, or the nearest `i128` if it is beyond them. Comparisons
    /// with numbers in the `i128` range give the same answer either way.
    #[must_use]
    pub fn saturating_i128(&self) -> i128 {
        self.to_i128().unwrap_or(if self.is_negative() {
            i128::MIN
        } else {
            i128::MAX
        })
    }

    /// The integer whose decimal digits are `text`: `0`, or digits that do
    /// not start with a zero.
    pub(crate) fn from_digits(text: String) -> Self {
        Self { text }
    }

    /// Whether the integer is below zero.
    #[must_use]
    pub fn is_negative(&self) -> bool {
        self.text.starts_with('-')
    }
}

impl From<i128> for Int {
    fn from(value: i128) -> Self {
        Self {
            text: value.to_string(),
        }
    }
}

impl fmt::Display for Int {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.text)
    }
}

impl Object {
    /// An empty object.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The value of a key.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.entries
            .iter()
            .find_map(|(name, value)| (name == key).then_some(value))
    }

    /// Whether the object has a key.
    #[must_use]
    pub fn contains_key(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    /// Set a key's value: a new key goes last, and a key the object has
    /// keeps its place.
    pub fn insert(&mut self, key: impl Into<String>, value: Value) {
        let key = key.into();
        match self.entries.iter_mut().find(|(name, _)| *name == key) {
            Some(entry) => entry.1 = value,
            None => self.entries.push((key, value)),
        }
    }

    /// The keys and values, in order.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &Value)> {
        self.entries
            .iter()
            .map(|(key, value)| (key.as_str(), value))
    }

    /// The keys, in order.
    pub fn keys(&self) -> impl Iterator<Item = &str> {
        self.entries.iter().map(|(key, _)| key.as_str())
    }

    /// The number of keys.
    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Whether the object has no keys.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Remove a key, and return its value if the object had it.
    pub fn remove(&mut self, key: &str) -> Option<Value> {
        let place = self.entries.iter().position(|(name, _)| name == key)?;
        Some(self.entries.remove(place).1)
    }
}

impl JsonError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }

    /// What is wrong, and for text that cannot be read, where.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for JsonError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for JsonError {}

impl From<i128> for Value {
    fn from(value: i128) -> Self {
        Self::Int(Int::from(value))
    }
}

impl From<&str> for Value {
    fn from(value: &str) -> Self {
        Self::String(value.to_owned())
    }
}

impl From<String> for Value {
    fn from(value: String) -> Self {
        Self::String(value)
    }
}

impl Value {
    /// The value of a key, if this is an object that has it.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.as_object()?.get(key)
    }

    /// The text, if this is a string.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(text) => Some(text),
            _ => None,
        }
    }

    /// The integer, if this is one.
    #[must_use]
    pub fn as_int(&self) -> Option<&Int> {
        match self {
            Self::Int(int) => Some(int),
            _ => None,
        }
    }

    /// The integer, if this is one that fits in an `i128`.
    #[must_use]
    pub fn as_i128(&self) -> Option<i128> {
        self.as_int()?.to_i128()
    }

    /// The items, if this is an array.
    #[must_use]
    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Self::Array(items) => Some(items),
            _ => None,
        }
    }

    /// The object, if this is one.
    #[must_use]
    pub fn as_object(&self) -> Option<&Object> {
        match self {
            Self::Object(object) => Some(object),
            _ => None,
        }
    }

    /// Whether this is `null`.
    #[must_use]
    pub fn is_null(&self) -> bool {
        matches!(self, Self::Null)
    }

    /// Write the value as `json.dumps(value, indent=indent, ensure_ascii=False,
    /// allow_nan=False)` does: on one line with `, ` and `: ` between items,
    /// or with `indent` spaces per level.
    ///
    /// # Errors
    ///
    /// If the value holds `NaN` or an infinity, which JSON has no form for.
    pub fn to_json(&self, indent: Option<usize>) -> Result<String, JsonError> {
        let mut out = String::new();
        write_value(&mut out, self, indent, 0)?;
        Ok(out)
    }
}

/// Read JSON text as `json.loads` does.
///
/// # Errors
///
/// If the text is not JSON. The message says what was expected, and the
/// line, column and character where, as Python's does.
pub fn parse(text: &str) -> Result<Value, JsonError> {
    let mut parser = Parser {
        text,
        bytes: text.as_bytes(),
        at: 0,
    };
    if text.starts_with('\u{feff}') {
        return Err(parser.error("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0));
    }
    parser.skip_whitespace();
    let value = parser.value(0)?;
    parser.skip_whitespace();
    if parser.at < parser.bytes.len() {
        return Err(parser.error("Extra data", parser.at));
    }
    Ok(value)
}

/// A recursive descent parser over the text's bytes. Every position it stops
/// at is a character boundary, because it only steps over ASCII bytes and
/// whole strings.
struct Parser<'a> {
    text: &'a str,
    bytes: &'a [u8],
    at: usize,
}

impl Parser<'_> {
    /// An error at a byte position, described as Python describes it: the
    /// line and column, and the character's index.
    fn error(&self, what: &str, at: usize) -> JsonError {
        let before = &self.text[..at];
        let line = before.matches('\n').count() + 1;
        let char_index = before.chars().count();
        let column = match before.rfind('\n') {
            Some(newline) => before[newline + 1..].chars().count() + 1,
            None => char_index + 1,
        };
        JsonError::new(format!(
            "{what}: line {line} column {column} (char {char_index})"
        ))
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn starts_with(&self, word: &str) -> bool {
        self.bytes[self.at..].starts_with(word.as_bytes())
    }

    fn value(&mut self, depth: usize) -> Result<Value, JsonError> {
        let start = self.at;
        let literal = [
            ("null", Value::Null),
            ("true", Value::Bool(true)),
            ("false", Value::Bool(false)),
            ("NaN", Value::Float(f64::NAN)),
            ("Infinity", Value::Float(f64::INFINITY)),
            ("-Infinity", Value::Float(f64::NEG_INFINITY)),
        ]
        .into_iter()
        .find(|(word, _)| self.starts_with(word));
        if let Some((word, value)) = literal {
            self.at += word.len();
            return Ok(value);
        }
        match self.peek() {
            Some(b'"') => Ok(Value::String(self.string()?)),
            Some(b'[' | b'{') if depth >= MAX_DEPTH => Err(self.error("Too deeply nested", start)),
            Some(b'[') => self.array(depth + 1),
            Some(b'{') => self.object(depth + 1),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(self.error("Expecting value", start)),
        }
    }

    fn array(&mut self, depth: usize) -> Result<Value, JsonError> {
        self.at += 1;
        let mut items = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.at += 1;
            return Ok(Value::Array(items));
        }
        loop {
            self.skip_whitespace();
            items.push(self.value(depth)?);
            self.skip_whitespace();
            match self.peek() {
                Some(b']') => {
                    self.at += 1;
                    return Ok(Value::Array(items));
                }
                Some(b',') => self.at += 1,
                _ => return Err(self.error("Expecting ',' delimiter", self.at)),
            }
        }
    }

    fn object(&mut self, depth: usize) -> Result<Value, JsonError> {
        self.at += 1;
        // Where each key is, so a repeated key replaces its value in place.
        let mut places: HashMap<String, usize> = HashMap::new();
        let mut entries: Vec<(String, Value)> = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(Value::Object(Object { entries }));
        }
        loop {
            self.skip_whitespace();
            if self.peek() != Some(b'"') {
                return Err(
                    self.error("Expecting property name enclosed in double quotes", self.at)
                );
            }
            let key = self.string()?;
            self.skip_whitespace();
            if self.peek() != Some(b':') {
                return Err(self.error("Expecting ':' delimiter", self.at));
            }
            self.at += 1;
            self.skip_whitespace();
            let value = self.value(depth)?;
            if let Some(&place) = places.get(&key) {
                entries[place].1 = value;
            } else {
                places.insert(key.clone(), entries.len());
                entries.push((key, value));
            }
            self.skip_whitespace();
            match self.peek() {
                Some(b'}') => {
                    self.at += 1;
                    return Ok(Value::Object(Object { entries }));
                }
                Some(b',') => self.at += 1,
                _ => return Err(self.error("Expecting ',' delimiter", self.at)),
            }
        }
    }

    /// `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?`: an integer without a
    /// fraction or an exponent, a float with either.
    fn number(&mut self) -> Result<Value, JsonError> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        match self.peek() {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => self.digits(),
            _ => return Err(self.error("Expecting value", start)),
        }
        let integer_end = self.at;
        if self.peek() == Some(b'.') && self.digit_at(self.at + 1) {
            self.at += 1;
            self.digits();
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let sign = usize::from(matches!(self.bytes.get(self.at + 1), Some(b'-' | b'+')));
            if self.digit_at(self.at + 1 + sign) {
                self.at += 1 + sign;
                self.digits();
            }
        }
        let text = &self.text[start..self.at];
        if self.at == integer_end {
            let digits = text.strip_prefix('-').unwrap_or(text);
            let text = if digits == "0" { digits } else { text };
            return Ok(Value::Int(Int {
                text: text.to_owned(),
            }));
        }
        // Rust's parse rounds to the nearest float, as Python's float() does.
        Ok(Value::Float(text.parse().unwrap_or(f64::NAN)))
    }

    fn digit_at(&self, at: usize) -> bool {
        self.bytes.get(at).is_some_and(u8::is_ascii_digit)
    }

    fn digits(&mut self) {
        while self.digit_at(self.at) {
            self.at += 1;
        }
    }

    fn string(&mut self) -> Result<String, JsonError> {
        let start = self.at;
        self.at += 1;
        let mut out = String::new();
        loop {
            // Copy the run of characters that need no decoding at once.
            let run = self.bytes[self.at..]
                .iter()
                .position(|&byte| byte == b'"' || byte == b'\\' || byte < 0x20);
            let Some(run) = run else {
                return Err(self.error("Unterminated string starting at", start));
            };
            out.push_str(&self.text[self.at..self.at + run]);
            self.at += run;
            match self.bytes[self.at] {
                b'"' => {
                    self.at += 1;
                    return Ok(out);
                }
                b'\\' => self.escape(&mut out, start)?,
                _ => return Err(self.error("Invalid control character at", self.at)),
            }
        }
    }

    /// The escape at the backslash, in the string that starts at `start`.
    fn escape(&mut self, out: &mut String, start: usize) -> Result<(), JsonError> {
        let at = self.at;
        let decoded = match self.bytes.get(at + 1) {
            Some(b'"') => '"',
            Some(b'\\') => '\\',
            Some(b'/') => '/',
            Some(b'b') => '\u{8}',
            Some(b'f') => '\u{c}',
            Some(b'n') => '\n',
            Some(b'r') => '\r',
            Some(b't') => '\t',
            Some(b'u') => return self.unicode_escape(out),
            Some(_) => return Err(self.error("Invalid \\escape", at)),
            None => return Err(self.error("Unterminated string starting at", start)),
        };
        out.push(decoded);
        self.at += 2;
        Ok(())
    }

    /// `\uXXXX`, or two of them for a character beyond U+FFFF.
    fn unicode_escape(&mut self, out: &mut String) -> Result<(), JsonError> {
        let at = self.at;
        let unit = self.hex_unit(at)?;
        let code = if (0xd800..0xdc00).contains(&unit) {
            let low = self
                .starts_with_at(at + 6, "\\u")
                .then(|| self.hex_unit(at + 6))
                .transpose()?
                .filter(|low| (0xdc00..0xe000).contains(low));
            let Some(low) = low else {
                return Err(self.error("Lone surrogate in \\uXXXX escape", at));
            };
            self.at += 6;
            0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00)
        } else {
            unit
        };
        let Some(char) = char::from_u32(code) else {
            return Err(self.error("Lone surrogate in \\uXXXX escape", at));
        };
        out.push(char);
        self.at += 6;
        Ok(())
    }

    fn starts_with_at(&self, at: usize, word: &str) -> bool {
        self.bytes
            .get(at..)
            .is_some_and(|rest| rest.starts_with(word.as_bytes()))
    }

    /// The four hex digits after the `\u` at `at`.
    fn hex_unit(&self, at: usize) -> Result<u32, JsonError> {
        self.bytes
            .get(at + 2..at + 6)
            .filter(|digits| digits.iter().all(u8::is_ascii_hexdigit))
            .and_then(|digits| u32::from_str_radix(std::str::from_utf8(digits).ok()?, 16).ok())
            .ok_or_else(|| self.error("Invalid \\uXXXX escape", at + 1))
    }
}

fn write_value(
    out: &mut String,
    value: &Value,
    indent: Option<usize>,
    level: usize,
) -> Result<(), JsonError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(int) => out.push_str(&int.text),
        Value::Float(float) => out.push_str(&float_repr(*float)?),
        Value::String(text) => write_string(out, text),
        Value::Array(items) => {
            write_items(out, ('[', ']'), items, indent, level, |out, item, level| {
                write_value(out, item, indent, level)
            })?;
        }
        Value::Object(object) => {
            let entries = &object.entries;
            write_items(
                out,
                ('{', '}'),
                entries,
                indent,
                level,
                |out, entry, level| {
                    write_string(out, &entry.0);
                    out.push_str(": ");
                    write_value(out, &entry.1, indent, level)
                },
            )?;
        }
    }
    Ok(())
}

/// An array's items or an object's entries, between its brackets.
fn write_items<T>(
    out: &mut String,
    (open, close): (char, char),
    items: &[T],
    indent: Option<usize>,
    level: usize,
    mut write: impl FnMut(&mut String, &T, usize) -> Result<(), JsonError>,
) -> Result<(), JsonError> {
    out.push(open);
    for (i, item) in items.iter().enumerate() {
        match indent {
            None if i > 0 => out.push_str(", "),
            None => {}
            Some(width) => {
                if i > 0 {
                    out.push(',');
                }
                out.push('\n');
                out.push_str(&" ".repeat(width * (level + 1)));
            }
        }
        write(out, item, level + 1)?;
    }
    if let (Some(width), false) = (indent, items.is_empty()) {
        out.push('\n');
        out.push_str(&" ".repeat(width * level));
    }
    out.push(close);
    Ok(())
}

/// A string with `"`, `\` and the control characters escaped, and every other
/// character as it is (`ensure_ascii=False`).
fn write_string(out: &mut String, text: &str) {
    out.push('"');
    for char in text.chars() {
        match char {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            char if char < '\u{20}' => {
                // Writing to a String cannot fail, so the Result is always Ok.
                let _ = write!(out, "\\u{:04x}", u32::from(char));
            }
            char => out.push(char),
        }
    }
    out.push('"');
}

/// The float in exponent notation, such as `-1.25e-7`, with the fewest
/// digits that read back as the same float and, of those, the digits nearest
/// to it, as Python's `repr` chooses them. Rust's `{:e}` finds the fewest
/// digits, but not always the nearest: `{:.*e}` rounds to the nearest at that
/// many digits, which reads back unless the float is next to a power of two.
fn shortest_nearest(float: f64) -> String {
    let shortest = format!("{float:e}");
    let digits = shortest
        .split('e')
        .next()
        .unwrap_or_default()
        .bytes()
        .filter(u8::is_ascii_digit)
        .count();
    let nearest = format!("{float:.*e}", digits.saturating_sub(1));
    if nearest.parse::<f64>() == Ok(float) {
        nearest
    } else {
        shortest
    }
}

/// A float as Python's `repr` writes it: the shortest digits that read back
/// as the same float, in positional notation from 1e-4 up to 1e16, and in
/// exponent notation outside, with a sign and at least two exponent digits.
///
/// # Errors
///
/// For `NaN` and the infinities.
pub fn float_repr(float: f64) -> Result<String, JsonError> {
    if !float.is_finite() {
        let name = if float.is_nan() {
            "nan"
        } else if float.is_sign_positive() {
            "inf"
        } else {
            "-inf"
        };
        return Err(JsonError::new(format!(
            "Out of range float values are not JSON compliant: {name}"
        )));
    }
    let scientific = shortest_nearest(float);
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i64 = exponent.parse().unwrap_or(0);
    let sign = if mantissa.starts_with('-') { "-" } else { "" };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    if !(-4..16).contains(&exponent) {
        let (first, rest) = digits.split_at(1);
        let point = if rest.is_empty() { "" } else { "." };
        let exponent_sign = if exponent.is_negative() { '-' } else { '+' };
        return Ok(format!(
            "{sign}{first}{point}{rest}e{exponent_sign}{:02}",
            exponent.unsigned_abs()
        ));
    }
    // How many digits come before the point: -3 to 16.
    let point = exponent + 1;
    let text = match usize::try_from(point) {
        // All the digits before the point: 1.0, 150.0, 1500000.0.
        Ok(point) if point >= digits.len() => {
            format!("{digits}{}.0", "0".repeat(point - digits.len()))
        }
        // Some before and some after: 1.5, 123.25.
        Ok(point) if point > 0 => format!("{}.{}", &digits[..point], &digits[point..]),
        // None before: 0.15, 0.0015.
        _ => {
            let zeros = usize::try_from(-point).unwrap_or_default();
            format!("0.{}{digits}", "0".repeat(zeros))
        }
    };
    Ok(format!("{sign}{text}"))
}
