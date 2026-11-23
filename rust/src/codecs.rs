//! Convert column values between text, integer ordinals and SQL literals.
//!
//! Every type the planner can split maps each value to an integer, its
//! ordinal, so that the order of ordinals is the order of the values (spec
//! section 2). The planner works on ordinals only; a codec reads values from
//! PostgreSQL's text form and writes them back to text and to SQL literals.
//! Ordinals are `i128`, so the planner's products of a row count and an
//! ordinal difference never overflow.
//!
//! ```
//! use skewsplit::codecs::codec_for;
//!
//! let codec = codec_for("date", None)?;
//! assert_eq!(codec.parse("2026-11-18")?, 20775);
//! assert_eq!(codec.literal(20775)?, "'2026-11-18'::date");
//! # Ok::<(), skewsplit::SpecError>(())
//! ```

use std::fmt::Write;

use crate::error::{ErrorCode, SpecError};

/// The smallest ordinal of `int8` and `numeric`.
pub const INT64_MIN: i128 = i64::MIN as i128;
/// The largest ordinal of `int8` and `numeric`.
pub const INT64_MAX: i128 = i64::MAX as i128;
/// The largest `numeric` scale.
pub const MAX_SCALE: u32 = 1000;
/// The supported types, in the order error messages list them.
pub const TYPE_NAMES: [&str; 7] = [
    "int2",
    "int4",
    "int8",
    "date",
    "timestamp",
    "timestamptz",
    "numeric",
];

// The largest int64 has 19 digits, so a value with more is out of range.
const MAX_DIGITS: usize = 19;
// How much of a value an error message quotes, in characters.
const SHOWN_CHARS: usize = 40;
const US_PER_SECOND: i128 = 1_000_000;
const US_PER_DAY: i128 = 86_400 * US_PER_SECOND;
// 0001-01-01 and 9999-12-31, in days since 1970-01-01.
const DATE_MIN: i128 = -719_162;
const DATE_MAX: i128 = 2_932_896;
// What PostgreSQL prints for infinite dates and timestamps and for NaN and
// infinite numerics. None of them has an ordinal.
const SPECIAL_VALUES: [&str; 4] = ["infinity", "+infinity", "-infinity", "nan"];

/// How a type maps values to ordinals.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    /// `int2`, `int4` and `int8`: the ordinal is the value.
    Integer,
    /// `numeric` with this scale: the ordinal is the value times 10^scale.
    Numeric(u32),
    /// `date`: days since 1970-01-01.
    Date,
    /// `timestamp` and `timestamptz`: microseconds since 1970-01-01 (UTC),
    /// with this suffix after every value (`+00` for `timestamptz`).
    Timestamp(&'static str),
}

/// The conversions for one column type.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Codec {
    type_name: &'static str,
    kind: Kind,
    min_ordinal: i128,
    max_ordinal: i128,
}

/// Return the codec for a column type and, for `numeric` only, its scale.
///
/// # Errors
///
/// `unsupported_type` if the type is not supported, `scale` is missing for
/// `numeric` or given for another type, or `scale` is outside 0 to 1000.
pub fn codec_for(type_name: &str, scale: Option<i64>) -> Result<Codec, SpecError> {
    if type_name == "numeric" {
        let Some(scale) = scale else {
            return Err(unsupported_type(
                "numeric needs its declared scale; unconstrained numeric is not supported",
            ));
        };
        return match u32::try_from(scale) {
            Ok(scale) if scale <= MAX_SCALE => Ok(Codec::new(
                "numeric",
                Kind::Numeric(scale),
                INT64_MIN,
                INT64_MAX,
            )),
            _ => Err(unsupported_type(format!(
                "numeric scale {scale} is outside 0 to {MAX_SCALE}"
            ))),
        };
    }
    let codec = match type_name {
        "int2" => Codec::integer("int2", 16),
        "int4" => Codec::integer("int4", 32),
        "int8" => Codec::integer("int8", 64),
        "date" => Codec::new("date", Kind::Date, DATE_MIN, DATE_MAX),
        "timestamp" => Codec::timestamp("timestamp", ""),
        "timestamptz" => Codec::timestamp("timestamptz", "+00"),
        _ => {
            return Err(unsupported_type(format!(
                "type {} is not supported; expected one of {}",
                quoted(type_name),
                TYPE_NAMES.join(", ")
            )));
        }
    };
    if scale.is_some() {
        return Err(unsupported_type(format!(
            "scale is only for numeric, not {type_name}"
        )));
    }
    Ok(codec)
}

impl Codec {
    const fn new(
        type_name: &'static str,
        kind: Kind,
        min_ordinal: i128,
        max_ordinal: i128,
    ) -> Self {
        Self {
            type_name,
            kind,
            min_ordinal,
            max_ordinal,
        }
    }

    /// A signed integer of `bits` bits.
    const fn integer(type_name: &'static str, bits: u32) -> Self {
        let half = 1_i128 << (bits - 1);
        Self::new(type_name, Kind::Integer, -half, half - 1)
    }

    /// The timestamps from 0001-01-01 00:00:00 to 9999-12-31 23:59:59.999999.
    const fn timestamp(type_name: &'static str, suffix: &'static str) -> Self {
        let low = DATE_MIN * US_PER_DAY;
        let high = (DATE_MAX + 1) * US_PER_DAY - 1;
        Self::new(type_name, Kind::Timestamp(suffix), low, high)
    }

    /// The PostgreSQL type name used in the input and in casts.
    #[must_use]
    pub fn type_name(&self) -> &'static str {
        self.type_name
    }

    /// The smallest ordinal of the type (spec section 2).
    #[must_use]
    pub fn min_ordinal(&self) -> i128 {
        self.min_ordinal
    }

    /// The largest ordinal of the type.
    #[must_use]
    pub fn max_ordinal(&self) -> i128 {
        self.max_ordinal
    }

    /// The scale of a `numeric` codec, or `None` for every other type.
    #[must_use]
    pub fn scale(&self) -> Option<u32> {
        match self.kind {
            Kind::Numeric(scale) => Some(scale),
            _ => None,
        }
    }

    /// Return the ordinal of a value in the type's text form.
    ///
    /// # Errors
    ///
    /// `invalid_value` if the text is not in the form, or
    /// `value_out_of_range` if its ordinal is outside the type's range.
    pub fn parse(&self, text: &str) -> Result<i128, SpecError> {
        let Some(ordinal) = self.ordinal_of(text) else {
            return Err(self.invalid(text));
        };
        if !self.contains(ordinal) {
            return Err(self.out_of_range(&shown(text)));
        }
        Ok(ordinal)
    }

    /// Return the canonical text form of an ordinal.
    ///
    /// # Errors
    ///
    /// `value_out_of_range` if the ordinal is outside the type's range.
    pub fn text(&self, ordinal: i128) -> Result<String, SpecError> {
        if !self.contains(ordinal) {
            return Err(self.out_of_range(&format!("ordinal {ordinal}")));
        }
        Ok(self.format(ordinal))
    }

    /// Return the SQL literal of an ordinal, as written in a predicate:
    /// numbers bare, dates and timestamps quoted with a cast (spec section 5).
    ///
    /// # Errors
    ///
    /// `value_out_of_range` if the ordinal is outside the type's range.
    pub fn literal(&self, ordinal: i128) -> Result<String, SpecError> {
        let text = self.text(ordinal)?;
        Ok(match self.kind {
            Kind::Integer | Kind::Numeric(_) => text,
            Kind::Date | Kind::Timestamp(_) => format!("'{text}'::{}", self.type_name),
        })
    }

    fn contains(&self, ordinal: i128) -> bool {
        (self.min_ordinal..=self.max_ordinal).contains(&ordinal)
    }

    /// The ordinal, which may be out of range, or `None` if the form is wrong.
    fn ordinal_of(&self, text: &str) -> Option<i128> {
        match self.kind {
            Kind::Integer => integer_ordinal(text),
            Kind::Numeric(scale) => numeric_ordinal(text, scale),
            Kind::Date => date_ordinal(text.as_bytes()),
            Kind::Timestamp(suffix) => timestamp_ordinal(text.strip_suffix(suffix)?.as_bytes()),
        }
    }

    /// The text form of an ordinal within the range.
    fn format(&self, ordinal: i128) -> String {
        match self.kind {
            Kind::Integer => ordinal.to_string(),
            Kind::Numeric(scale) => numeric_text(ordinal, scale),
            Kind::Date => date_text(ordinal),
            Kind::Timestamp(suffix) => timestamp_text(ordinal) + suffix,
        }
    }

    /// The text form in words, for error messages.
    fn form(&self) -> String {
        match self.kind {
            Kind::Integer => "an integer such as 42 or -5".to_owned(),
            Kind::Numeric(scale) => {
                format!("a number with at most {scale} digits after the point, such as 12.5")
            }
            Kind::Date => "a calendar date such as 2026-11-18".to_owned(),
            Kind::Timestamp(suffix) => {
                format!("a timestamp such as 2026-11-18 15:30:00.25{suffix}")
            }
        }
    }

    fn invalid(&self, text: &str) -> SpecError {
        let lowered = text.trim().to_lowercase();
        let reason = if SPECIAL_VALUES.contains(&lowered.as_str()) {
            "special values such as infinity and NaN have no ordinal and cannot be planned"
                .to_owned()
        } else if lowered.ends_with(" bc") {
            "values before year 1 (BC) are not supported".to_owned()
        } else if is_negative_zero(text) {
            "zero is written without a minus sign".to_owned()
        } else {
            format!("expected {}", self.form())
        };
        let message = format!(
            "{} is not a valid {} value: {reason}",
            shown(text),
            self.type_name
        );
        SpecError::new(ErrorCode::InvalidValue, message)
    }

    fn out_of_range(&self, what: &str) -> SpecError {
        let low = self.format(self.min_ordinal);
        let high = self.format(self.max_ordinal);
        let message = format!(
            "{what} is outside the {} range, {low} to {high}",
            self.type_name
        );
        SpecError::new(ErrorCode::ValueOutOfRange, message)
    }
}

fn unsupported_type(message: impl Into<String>) -> SpecError {
    SpecError::new(ErrorCode::UnsupportedType, message)
}

/// Whether the text is one or more ASCII digits. `char::is_numeric` would
/// also accept the digits of other scripts.
fn is_digits(bytes: &[u8]) -> bool {
    !bytes.is_empty() && bytes.iter().all(u8::is_ascii_digit)
}

/// The value of a run of ASCII digits short enough to fit.
fn number(digits: &[u8]) -> i128 {
    digits
        .iter()
        .fold(0, |value, digit| value * 10 + i128::from(digit - b'0'))
}

/// Split off an optional minus sign: `(negative, the rest)`.
fn sign(text: &str) -> (bool, &str) {
    match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text),
    }
}

/// Whether the digits are `0` or start with a digit other than `0`.
fn is_whole(digits: &str) -> bool {
    is_digits(digits.as_bytes()) && (digits == "0" || !digits.starts_with('0'))
}

/// `-?(0|[1-9][0-9]*)`, but not `-0`.
fn integer_ordinal(text: &str) -> Option<i128> {
    let (negative, digits) = sign(text);
    if !is_whole(digits) || (negative && digits == "0") {
        return None;
    }
    if digits.len() > MAX_DIGITS {
        return Some(INT64_MAX + 1);
    }
    let value = number(digits.as_bytes());
    Some(if negative { -value } else { value })
}

/// `-?(0|[1-9][0-9]*)(\.[0-9]+)?` with at most `scale` digits after the
/// point, and not a negative zero.
fn numeric_ordinal(text: &str, scale: u32) -> Option<i128> {
    let (negative, unsigned) = sign(text);
    let (whole, fraction) = match unsigned.split_once('.') {
        Some((whole, fraction)) if is_digits(fraction.as_bytes()) => (whole, fraction),
        Some(_) => return None,
        None => (unsigned, ""),
    };
    let scale = scale as usize;
    if !is_whole(whole) || fraction.len() > scale {
        return None;
    }
    let scaled = format!("{whole}{fraction:0<scale$}");
    let digits = scaled.trim_start_matches('0');
    if negative && digits.is_empty() {
        return None;
    }
    if digits.len() > MAX_DIGITS {
        return Some(INT64_MAX + 1);
    }
    let value = number(digits.as_bytes());
    Some(if negative { -value } else { value })
}

/// `YYYY-MM-DD`, a real date in the proleptic Gregorian calendar.
fn date_ordinal(bytes: &[u8]) -> Option<i128> {
    let [y1, y2, y3, y4, b'-', m1, m2, b'-', d1, d2] = *bytes else {
        return None;
    };
    let (year, month, day) = ([y1, y2, y3, y4], [m1, m2], [d1, d2]);
    if !(is_digits(&year) && is_digits(&month) && is_digits(&day)) {
        return None;
    }
    days_of(number(&year), number(&month), number(&day))
}

/// `YYYY-MM-DD HH:MM:SS`, optionally with `.` and 1 to 6 digits, with the
/// suffix already removed.
fn timestamp_ordinal(bytes: &[u8]) -> Option<i128> {
    if bytes.len() < 19 {
        return None;
    }
    let (date, rest) = bytes.split_at(10);
    let days = date_ordinal(date)?;
    let [b' ', h1, h2, b':', n1, n2, b':', s1, s2] = rest[..9] else {
        return None;
    };
    let (hour, minute, second) = ([h1, h2], [n1, n2], [s1, s2]);
    if !(is_digits(&hour) && is_digits(&minute) && is_digits(&second)) {
        return None;
    }
    let (hour, minute, second) = (number(&hour), number(&minute), number(&second));
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let micros = match &rest[9..] {
        [] => 0,
        [b'.', fraction @ ..] if is_digits(fraction) && fraction.len() <= 6 => {
            // `.25` is 250000 microseconds: pad the digits to six.
            let padded: Vec<u8> = fraction.iter().copied().chain([b'0'; 6]).take(6).collect();
            number(&padded)
        }
        _ => return None,
    };
    let seconds = days * 86_400 + hour * 3_600 + minute * 60 + second;
    Some(seconds * US_PER_SECOND + micros)
}

/// Days since 1970-01-01, or `None` if the date is not in the calendar.
fn days_of(year: i128, month: i128, day: i128) -> Option<i128> {
    let length = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => return None,
    };
    if year < 1 || !(1..=length).contains(&day) {
        return None;
    }
    Some(days_from_civil(year, month, day))
}

fn is_leap(year: i128) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

/// Days since 1970-01-01 of a date in the proleptic Gregorian calendar, by
/// Howard Hinnant's `days_from_civil`. Its years start in March, so that the
/// leap day is the last day of a year.
fn days_from_civil(year: i128, month: i128, day: i128) -> i128 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year - era * 400;
    let day_of_year = (153 * ((month + 9) % 12) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// The date of a day since 1970-01-01: the inverse of `days_from_civil`.
fn civil_from_days(days: i128) -> (i128, i128, i128) {
    let days = days + 719_468;
    let era = days.div_euclid(146_097);
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_from_march = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_from_march + 2) / 5 + 1;
    let month = if month_from_march < 10 {
        month_from_march + 3
    } else {
        month_from_march - 9
    };
    let year = year_of_era + era * 400 + i128::from(month <= 2);
    (year, month, day)
}

fn date_text(days: i128) -> String {
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}")
}

fn timestamp_text(ordinal: i128) -> String {
    let (days, micros) = (
        ordinal.div_euclid(US_PER_DAY),
        ordinal.rem_euclid(US_PER_DAY),
    );
    let (seconds, fraction) = (micros / US_PER_SECOND, micros % US_PER_SECOND);
    let (hour, minute, second) = (seconds / 3_600, seconds / 60 % 60, seconds % 60);
    let mut text = format!("{} {hour:02}:{minute:02}:{second:02}", date_text(days));
    if fraction != 0 {
        text.push('.');
        text.push_str(format!("{fraction:06}").trim_end_matches('0'));
    }
    text
}

/// The ordinal with exactly `scale` digits after the point, built from its
/// digits: 10^scale does not fit in an `i128` for scales above 38.
fn numeric_text(ordinal: i128, scale: u32) -> String {
    if scale == 0 {
        return ordinal.to_string();
    }
    let scale = scale as usize;
    let digits = format!("{:0>width$}", ordinal.unsigned_abs(), width = scale + 1);
    let (whole, fraction) = digits.split_at(digits.len() - scale);
    let sign = if ordinal < 0 { "-" } else { "" };
    format!("{sign}{whole}.{fraction}")
}

/// `-0+(\.0+)?`: a zero with a minus sign.
fn is_negative_zero(text: &str) -> bool {
    let Some(rest) = text.strip_prefix('-') else {
        return false;
    };
    let (whole, fraction) = rest.split_once('.').unwrap_or((rest, "0"));
    [whole, fraction]
        .iter()
        .all(|part| !part.is_empty() && part.bytes().all(|byte| byte == b'0'))
}

/// Quote a value for a message, shortened if it is long.
fn shown(text: &str) -> String {
    if text.chars().count() <= SHOWN_CHARS {
        quoted(text)
    } else {
        let start: String = text.chars().take(SHOWN_CHARS).collect();
        format!("{}...", quoted(&start))
    }
}

/// Quote text as Python's `repr` does for the text people usually write: in
/// single quotes, or in double quotes if it has a single quote and no double
/// one, with backslashes, that quote and control characters escaped. The
/// wording of messages is not part of the contract (spec section 9).
fn quoted(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for char in text.chars() {
        match char {
            '\\' => out.push_str("\\\\"),
            char if char == quote => {
                out.push('\\');
                out.push(char);
            }
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            char if char.is_control() => {
                // Writing to a String cannot fail, so the Result is always Ok.
                let _ = write!(out, "\\x{:02x}", u32::from(char));
            }
            char => out.push(char),
        }
    }
    out.push(quote);
    out
}
