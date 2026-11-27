//! Build the planner input from a sample of the column (spec section 14).
//!
//! The sample is each distinct value with the number of sampled rows that
//! have it, so an adapter can count them in the database and fetch one row
//! per value. All arithmetic is on integers, so every implementation gets the
//! same input from the same sample.
//!
//! ```
//! use skewsplit::{json, planner::plan, sample::from_sample};
//!
//! let data = json::parse(
//!     r#"{"column": "id", "type": "int4", "partitions": 2, "rows_estimate": 100,
//!         "sample_null_rows": 0,
//!         "sample_values": [{"value": "1", "rows": 1}, {"value": "9", "rows": 1}]}"#,
//! )?;
//! let input = from_sample(&data)?;
//! assert_eq!(input.get("null_rows"), Some(&json::Value::from(0)));
//! assert_eq!(plan(&input)?.total_rows, 100);
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```

use std::collections::{BTreeMap, BTreeSet};

use crate::codecs::{Codec, INT64_MAX, codec_for};
use crate::error::{ErrorCode, SpecError};
use crate::inputs::{self, Kind, invalid_input, parse_input, scale_of};
use crate::json::{Int, Object, Value};

const REQUIRED: [&str; 6] = [
    "column",
    "type",
    "partitions",
    "rows_estimate",
    "sample_null_rows",
    "sample_values",
];
const OPTIONAL: [&str; 1] = ["scale"];
/// A common value holds at least a thousandth of the ordinary sampled rows,
/// and the histogram has at most this many buckets.
pub const RESOLUTION: i128 = 1000;
/// A value sampled once says nothing about how common it is.
const MIN_COMMON_ROWS: i128 = 2;

/// Values as `(ordinal, sampled rows)`.
type Values = Vec<(i128, i128)>;

/// Convert a decoded JSON sample of a column into a planner input (spec
/// section 1), as a JSON object in the order of that section.
///
/// # Errors
///
/// With the code of spec section 14, or of section 9 if the result is not a
/// valid planner input, for the first problem found, in the order the Python
/// core checks them.
pub fn from_sample(data: &Value) -> Result<Value, SpecError> {
    let fields = shape(data)?;
    let type_name = fields
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let scale = fields.get("scale").and_then(Value::as_int);
    let codec = codec_for(type_name, scale_of(type_name, scale)?)?;
    let entries = fields
        .get("sample_values")
        .and_then(Value::as_array)
        .unwrap_or_default();
    let (total, nulls, size) = counts(fields, entries)?;
    let (values, special_rows) = read_values(&codec, entries)?;

    // count × T / n, rounded half up; 0 for an empty sample. With every
    // count, T and n at most 2^63 - 1, 2 × count × T + n is below 2^127, so
    // it fits in an i128.
    let scale = |count: i128| {
        if size == 0 {
            0
        } else {
            (2 * count * total + size) / (2 * size)
        }
    };
    let ordinary_rows: i128 = values.iter().map(|&(_, rows)| rows).sum();
    let (common, rest): (Values, Values) = values
        .into_iter()
        .partition(|&(_, rows)| rows >= MIN_COMMON_ROWS && rows * RESOLUTION >= ordinary_rows);
    let (null_rows, unestimated) = (scale(nulls), scale(special_rows));
    let mcv_rows: Vec<i128> = common.iter().map(|&(_, rows)| scale(rows).max(1)).collect();
    // The values were read by the codec, so their text cannot fail.
    let text = |ordinal: i128| Value::from(codec.text(ordinal).unwrap_or_default());
    let mcv = common
        .iter()
        .zip(&mcv_rows)
        .map(|(&(value, _), &rows)| {
            let mut entry = Object::new();
            entry.insert("value", text(value));
            entry.insert("rows", Value::from(rows));
            Value::Object(entry)
        })
        .collect();
    let histogram = if rest.is_empty() {
        Value::Null
    } else {
        let rows = (total - null_rows - mcv_rows.iter().sum::<i128>() - unestimated).max(0);
        let mut histogram = Object::new();
        histogram.insert(
            "bounds",
            Value::Array(bounds(&rest).into_iter().map(text).collect()),
        );
        histogram.insert("rows", Value::from(rows));
        Value::Object(histogram)
    };
    let field = |key: &str| fields.get(key).cloned().unwrap_or(Value::Null);
    let mut result = Object::new();
    result.insert("column", field("column"));
    result.insert("type", field("type"));
    if fields.contains_key("scale") {
        result.insert("scale", field("scale"));
    }
    result.insert("partitions", field("partitions"));
    result.insert("null_rows", Value::from(null_rows));
    result.insert("mcv", Value::Array(mcv));
    result.insert("histogram", histogram);
    let mut source = Object::new();
    source.insert("statistics", Value::from("sample"));
    source.insert("rows_estimate", Value::from(total));
    source.insert("sample_rows", Value::from(size));
    source.insert("unestimated_rows", Value::from(unestimated));
    source.insert("warnings", Value::Array(warnings(unestimated)));
    result.insert("source", Value::Object(source));
    let result = Value::Object(result);
    parse_input(&result)?;
    Ok(result)
}

/// The table's estimated rows, the sampled NULL rows and the sample size,
/// once each count is in its range.
fn counts(fields: &Object, entries: &[Value]) -> Result<(i128, i128, i128), SpecError> {
    // shape checked that these are integers.
    let count = |value: Option<&Value>| {
        value
            .and_then(Value::as_int)
            .map_or(0, Int::saturating_i128)
    };
    let (total, nulls) = (
        count(fields.get("rows_estimate")),
        count(fields.get("sample_null_rows")),
    );
    let rows_of = |entry: &Value| count(entry.get("rows"));
    if total < 0 || nulls < 0 {
        return Err(fail(
            ErrorCode::InvalidRows,
            "rows_estimate and sample_null_rows must not be negative",
        ));
    }
    if entries.iter().any(|entry| rows_of(entry) < 1) {
        return Err(fail(
            ErrorCode::InvalidRows,
            "every sampled value needs at least 1 row",
        ));
    }
    // Every count is at least 0 here, so a sum that saturates is too large too.
    let size = entries
        .iter()
        .fold(nulls, |sum, entry| sum.saturating_add(rows_of(entry)));
    if total > INT64_MAX || size > INT64_MAX {
        return Err(fail(
            ErrorCode::InvalidRows,
            format!("rows_estimate and the sample size must be at most {INT64_MAX}"),
        ));
    }
    Ok((total, nulls, size))
}

/// The ordinary values as `(ordinal, rows)` in order, and the special
/// values' rows, from the entries in the order given.
fn read_values(codec: &Codec, entries: &[Value]) -> Result<(Values, i128), SpecError> {
    let special = codec.special_values();
    let mut ordinary = BTreeMap::new();
    let mut seen = BTreeSet::new();
    let mut special_rows = 0;
    for entry in entries {
        let text = entry
            .get("value")
            .and_then(Value::as_str)
            .unwrap_or_default();
        // The sample size fits in an i64, so every entry's rows do too.
        let rows = entry
            .get("rows")
            .and_then(Value::as_i128)
            .unwrap_or_default();
        if special.contains(&text) {
            if !seen.insert(text) {
                return Err(duplicate(text));
            }
            special_rows += rows;
            continue;
        }
        let ordinal = codec.parse(text)?;
        if ordinary.insert(ordinal, rows).is_some() {
            return Err(duplicate(&codec.text(ordinal).unwrap_or_default()));
        }
    }
    // A BTreeMap iterates its keys in order, so the values come sorted.
    Ok((ordinary.into_iter().collect(), special_rows))
}

/// The histogram bounds of the values, each repeated as often as it was
/// sampled. With `h` values in all and `m` buckets, bound `j` is the value at
/// position `j × (h - 1) / m`, as `ANALYZE` places them.
fn bounds(values: &[(i128, i128)]) -> Vec<i128> {
    let ends: Vec<i128> = values
        .iter()
        .scan(0, |sum, &(_, rows)| {
            *sum += rows;
            Some(*sum)
        })
        .collect();
    let count = ends.last().copied().unwrap_or_default();
    let buckets = (count - 1).clamp(1, RESOLUTION);
    // The value at a position is the first whose running total is above it:
    // partition_point counts the ends at or below the position, as Python's
    // bisect_right does.
    (0..=buckets)
        .map(|j| values[ends.partition_point(|&end| end <= j * (count - 1) / buckets)].0)
        .collect()
}

fn warnings(unestimated: i128) -> Vec<Value> {
    if unestimated == 0 {
        return Vec::new();
    }
    let mut warning = Object::new();
    warning.insert("code", Value::from("special_values_left_out"));
    warning.insert("rows", Value::from(unestimated));
    warning.insert(
        "message",
        Value::from(format!(
            "about {unestimated} rows at special values such as infinity are left out of the estimates; the predicates still read them"
        )),
    );
    vec![Value::Object(warning)]
}

/// Check the keys and the JSON type of every field this conversion reads.
fn shape(data: &Value) -> Result<&Object, SpecError> {
    let Some(fields) = data.as_object() else {
        return Err(invalid_input("the sample must be a JSON object"));
    };
    inputs::keys(fields, &REQUIRED, &OPTIONAL, "the sample")?;
    inputs::expect(fields, "type", Kind::String)?;
    for key in ["scale", "rows_estimate", "sample_null_rows"] {
        if fields.contains_key(key) {
            inputs::expect(fields, key, Kind::Integer)?;
        }
    }
    let entries = fields.get("sample_values").and_then(Value::as_array);
    if !entries.is_some_and(|entries| entries.iter().all(is_entry)) {
        return Err(invalid_input(
            "sample_values must be a list of objects with a string value and an integer rows",
        ));
    }
    Ok(fields)
}

fn is_entry(entry: &Value) -> bool {
    entry.as_object().is_some_and(|object| {
        object.len() == 2
            && object.get("value").and_then(Value::as_str).is_some()
            && object.get("rows").and_then(Value::as_int).is_some()
    })
}

fn duplicate(text: &str) -> SpecError {
    fail(
        ErrorCode::DuplicateValue,
        format!("{text} appears more than once in sample_values"),
    )
}

fn fail(code: ErrorCode, message: impl Into<String>) -> SpecError {
    SpecError::new(code, message)
}
