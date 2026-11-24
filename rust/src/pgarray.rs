//! PostgreSQL's array text, as `pg_stats` shows it (spec section 12).
//!
//! The adapters read `most_common_vals` and `histogram_bounds` from
//! `pg_stats` as text, in PostgreSQL's output format for arrays. These
//! functions turn it into values. The planner itself never sees array text.
//!
//! ```
//! use skewsplit::codecs::codec_for;
//! use skewsplit::pgarray::parse_values;
//!
//! let codec = codec_for("numeric", Some(2)).unwrap();
//! assert_eq!(parse_values("[0:1]={1.5,2.25}", &codec).unwrap(), ["1.50", "2.25"]);
//! ```

use crate::codecs::{Codec, quoted};
use crate::error::{ErrorCode, SpecError};

const INT4_MIN: i64 = -2_147_483_648;
const INT4_MAX: i64 = 2_147_483_647;
/// Error messages show at most this many characters of the text.
const SHOWN_CHARS: usize = 60;
/// The characters an unquoted element cannot hold: braces, the comma, the
/// double quote, the backslash, space, tab, line feed, carriage return,
/// vertical tab and form feed.
const SPECIAL: &[u8] = b"{},\"\\ \t\n\r\x0b\x0c";

/// Split one-dimensional array text into its elements; `None` is a NULL
/// element.
///
/// ```
/// use skewsplit::pgarray::parse_array;
///
/// let items = parse_array(r#"{7,"a b",NULL}"#).unwrap();
/// assert_eq!(items, [Some("7".to_owned()), Some("a b".to_owned()), None]);
/// ```
///
/// # Errors
///
/// `invalid_array` if the text is not in the format of spec section 12.
pub fn parse_array(text: &str) -> Result<Vec<Option<String>>, SpecError> {
    let prefix = prefix(text);
    let mut pos = prefix.map_or(0, |(_, _, end)| end);
    if !text[pos..].starts_with('{') {
        return Err(invalid(
            text,
            "an array starts with '{', after an optional [L:U]= prefix",
        ));
    }
    pos += 1;
    let mut items = Vec::new();
    if text[pos..].starts_with('}') {
        pos += 1;
    } else {
        loop {
            let (item, end) = element(text, pos)?;
            items.push(item);
            pos = end;
            if !text[pos..].starts_with(',') {
                break;
            }
            pos += 1;
        }
        if !text[pos..].starts_with('}') {
            return Err(invalid(
                text,
                "elements are separated by ',' and end with '}'",
            ));
        }
        pos += 1;
    }
    if pos != text.len() {
        return Err(invalid(text, "nothing may follow the closing '}'"));
    }
    if let Some((lower, upper, _)) = prefix {
        // An array this long never fits in memory, so the count fits.
        let count = i64::try_from(items.len()).unwrap_or(i64::MAX);
        let fits = match (lower, upper) {
            (Some(lower), Some(upper)) => {
                INT4_MIN <= lower && lower <= upper && upper <= INT4_MAX
                    // Both are int4 here, so this cannot overflow.
                    && upper - lower + 1 == count
            }
            // A bound too long for an i64 is out of the int4 range.
            _ => false,
        };
        if !fits {
            return Err(invalid(
                text,
                "the [L:U] prefix must match the number of elements",
            ));
        }
    }
    Ok(items)
}

/// Split array text from `pg_stats` into its elements, which are never NULL.
///
/// # Errors
///
/// `invalid_array` for bad array text or a NULL element.
pub fn parse_elements(text: &str) -> Result<Vec<String>, SpecError> {
    parse_array(text)?
        .into_iter()
        .collect::<Option<Vec<String>>>()
        .ok_or_else(|| invalid(text, "pg_stats arrays never hold NULL"))
}

/// Read array text as values of the codec's type, in their output form
/// (spec section 2).
///
/// # Errors
///
/// `invalid_array` for bad array text or a NULL element, or the codec's
/// `invalid_value` or `value_out_of_range` for the first element that is not a
/// value of the type.
pub fn parse_values(text: &str, codec: &Codec) -> Result<Vec<String>, SpecError> {
    parse_elements(text)?
        .iter()
        .map(|item| codec.text(codec.parse(item)?))
        .collect()
}

/// The `[L:U]=` prefix at the start of the text: its bounds, `None` for one
/// too long for an `i64`, and the byte offset after it.
fn prefix(text: &str) -> Option<(Option<i64>, Option<i64>, usize)> {
    let rest = text.strip_prefix('[')?;
    let (lower, rest) = integer(rest)?;
    let rest = rest.strip_prefix(':')?;
    let (upper, rest) = integer(rest)?;
    let rest = rest.strip_prefix("]=")?;
    Some((
        lower.parse().ok(),
        upper.parse().ok(),
        text.len() - rest.len(),
    ))
}

/// An integer in the form `-?(0|[1-9][0-9]*)` at the start of the text, and
/// the text after it.
fn integer(text: &str) -> Option<(&str, &str)> {
    let sign = usize::from(text.starts_with('-'));
    let end = match text.as_bytes().get(sign) {
        Some(b'0') => sign + 1,
        Some(b'1'..=b'9') => sign + text[sign..].bytes().take_while(u8::is_ascii_digit).count(),
        _ => return None,
    };
    Some(text.split_at(end))
}

/// Read the element at byte offset `pos`; return it and the offset after it.
fn element(text: &str, pos: usize) -> Result<(Option<String>, usize), SpecError> {
    let rest = &text[pos..];
    if let Some(inside) = rest.strip_prefix('"') {
        let mut item = String::new();
        let mut chars = inside.char_indices();
        while let Some((offset, char)) = chars.next() {
            match char {
                // The quotes are one byte each.
                '"' => return Ok((Some(item), pos + 1 + offset + 1)),
                '\\' => match chars.next() {
                    Some((_, escaped)) => item.push(escaped),
                    None => break,
                },
                char => item.push(char),
            }
        }
        // No closing quote: the text is not an element, quoted or not.
    }
    // Every special character is ASCII, so the word ends on a char boundary.
    let length = rest
        .bytes()
        .take_while(|byte| !SPECIAL.contains(byte))
        .count();
    if length == 0 {
        let offset = text[..pos].chars().count();
        return Err(invalid(
            text,
            &format!("no valid element at offset {offset}"),
        ));
    }
    let word = &rest[..length];
    let item = (!word.eq_ignore_ascii_case("NULL")).then(|| word.to_owned());
    Ok((item, pos + length))
}

fn invalid(text: &str, reason: &str) -> SpecError {
    let shown = if text.chars().count() <= SHOWN_CHARS {
        quoted(text)
    } else {
        let start: String = text.chars().take(SHOWN_CHARS - 3).collect();
        quoted(&(start + "..."))
    };
    SpecError::new(
        ErrorCode::InvalidArray,
        format!("{shown} is not array text: {reason}"),
    )
}
