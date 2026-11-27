//! Read and check the planner input (spec sections 1 and 9).
//!
//! ```
//! use skewsplit::{inputs::parse_input, json};
//!
//! let data = json::parse(
//!     r#"{"column": "id", "type": "int4", "partitions": 2, "null_rows": 0,
//!         "mcv": [{"value": "7", "rows": 40}],
//!         "histogram": {"bounds": ["1", "101"], "rows": 100}}"#,
//! )?;
//! let input = parse_input(&data)?;
//! assert_eq!(input.mcv(), [(7, 40)]);
//! assert_eq!(input.bounds(), [1, 101]);
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```

use std::collections::BTreeSet;

use crate::codecs::{Codec, INT64_MAX, codec_for};
use crate::error::{ErrorCode, SpecError};
use crate::json::{Int, Object, Value};

/// The most predicates other than the NULL predicate that a plan may have.
pub const MAX_PARTITIONS: i128 = 10_000;

const REQUIRED: [&str; 6] = [
    "column",
    "type",
    "partitions",
    "null_rows",
    "mcv",
    "histogram",
];
const OPTIONAL: [&str; 2] = ["scale", "source"];

/// A checked planner input, with values as ordinals. Only [`parse_input`]
/// makes one, so every `PlanInput` has passed the checks of spec section 9.
#[derive(Clone, Debug, PartialEq)]
pub struct PlanInput {
    column: String,
    codec: Codec,
    partitions: i128,
    null_rows: i128,
    mcv: Vec<(i128, i128)>,
    bounds: Vec<i128>,
    histogram_rows: i128,
    source: Value,
}

impl PlanInput {
    /// The column name, unquoted.
    #[must_use]
    pub fn column(&self) -> &str {
        &self.column
    }

    /// The codec of the column's type.
    #[must_use]
    pub fn codec(&self) -> &Codec {
        &self.codec
    }

    /// `N`, the most non-NULL predicates the plan may have: 1 to 10000.
    #[must_use]
    pub fn partitions(&self) -> i128 {
        self.partitions
    }

    /// Rows where the column is NULL.
    #[must_use]
    pub fn null_rows(&self) -> i128 {
        self.null_rows
    }

    /// The most common values as `(ordinal, rows)`, sorted by ordinal.
    #[must_use]
    pub fn mcv(&self) -> &[(i128, i128)] {
        &self.mcv
    }

    /// The histogram bounds as ordinals, or none without a histogram.
    #[must_use]
    pub fn bounds(&self) -> &[i128] {
        &self.bounds
    }

    /// The rows the histogram covers, 0 without one.
    #[must_use]
    pub fn histogram_rows(&self) -> i128 {
        self.histogram_rows
    }

    /// The input's `source`, copied to the plan: `null` if it has none.
    #[must_use]
    pub fn source(&self) -> &Value {
        &self.source
    }
}

/// Check a decoded JSON input and convert its values to ordinals.
///
/// # Errors
///
/// With the code of spec section 9 for the first problem found, in the order
/// the Python core checks them.
pub fn parse_input(data: &Value) -> Result<PlanInput, SpecError> {
    let fields = shape(data)?;
    let text = |key: &str| fields.get(key).and_then(Value::as_str).unwrap_or_default();
    let int = |key: &str| fields.get(key).and_then(Value::as_int);
    let column = text("column");
    if column.is_empty() || column.contains('\0') {
        return Err(fail(
            ErrorCode::InvalidColumn,
            "the column name must not be empty or contain U+0000",
        ));
    }
    let type_name = text("type");
    let codec = codec_for(type_name, scale_of(type_name, int("scale"))?)?;
    let partitions = int("partitions").map_or(0, Int::saturating_i128);
    if !(1..=MAX_PARTITIONS).contains(&partitions) {
        return Err(fail(
            ErrorCode::InvalidPartitions,
            format!(
                "partitions is {}; it must be 1 to {MAX_PARTITIONS}",
                int("partitions").map(Int::to_string).unwrap_or_default()
            ),
        ));
    }
    let histogram = fields.get("histogram").and_then(Value::as_object);
    let histogram_rows = histogram.and_then(|h| h.get("rows")).map_or(0, rows_of);
    let mcv = fields
        .get("mcv")
        .and_then(Value::as_array)
        .unwrap_or_default();
    let mcv_rows: Vec<i128> = mcv
        .iter()
        .map(|entry| entry.get("rows").map_or(0, rows_of))
        .collect();
    let null_rows = int("null_rows").map_or(0, Int::saturating_i128);
    if null_rows < 0 || histogram_rows < 0 {
        return Err(fail(
            ErrorCode::InvalidRows,
            "row counts must not be negative",
        ));
    }
    if mcv_rows.iter().any(|&rows| rows < 1) {
        return Err(fail(
            ErrorCode::InvalidRows,
            "every common value needs at least 1 row",
        ));
    }
    // Every count is at least 0 here, so a sum that saturates is too large too.
    let total = mcv_rows
        .iter()
        .fold(null_rows.saturating_add(histogram_rows), |sum, &rows| {
            sum.saturating_add(rows)
        });
    if total > INT64_MAX {
        return Err(fail(
            ErrorCode::InvalidRows,
            format!("the total row count exceeds {INT64_MAX}"),
        ));
    }
    let mut values = Vec::with_capacity(mcv.len());
    for (entry, rows) in mcv.iter().zip(mcv_rows) {
        let value = entry
            .get("value")
            .and_then(Value::as_str)
            .unwrap_or_default();
        values.push((codec.parse(value)?, rows));
    }
    values.sort_unstable();
    let bounds = match histogram
        .and_then(|h| h.get("bounds"))
        .and_then(Value::as_array)
    {
        Some(bounds) => bounds
            .iter()
            .map(|bound| codec.parse(bound.as_str().unwrap_or_default()))
            .collect::<Result<Vec<_>, _>>()?,
        None => Vec::new(),
    };
    check_values(&codec, &values, &bounds, histogram.is_some())?;
    Ok(PlanInput {
        column: column.to_owned(),
        codec,
        partitions,
        null_rows,
        mcv: values,
        bounds,
        histogram_rows,
        source: fields.get("source").cloned().unwrap_or(Value::Null),
    })
}

/// The scale to give `codec_for`. A scale beyond `i64` is refused here, with
/// the message `codec_for` would give it, since `codec_for` takes an `i64`.
pub(crate) fn scale_of(type_name: &str, scale: Option<&Int>) -> Result<Option<i64>, SpecError> {
    let Some(scale) = scale else {
        return Ok(None);
    };
    match scale.to_i128().and_then(|scale| i64::try_from(scale).ok()) {
        Some(scale) => Ok(Some(scale)),
        None if type_name == "numeric" => Err(fail(
            ErrorCode::UnsupportedType,
            format!("numeric scale {scale} is outside 0 to 1000"),
        )),
        // Any scale gives the same error for another type, which does not name it.
        None => Ok(Some(0)),
    }
}

fn rows_of(value: &Value) -> i128 {
    value.as_int().map_or(0, Int::saturating_i128)
}

fn check_values(
    codec: &Codec,
    mcv: &[(i128, i128)],
    bounds: &[i128],
    has_histogram: bool,
) -> Result<(), SpecError> {
    // The values are in range, so their text cannot fail.
    let text = |ordinal: i128| codec.text(ordinal).unwrap_or_default();
    if let Some(pair) = mcv.windows(2).find(|pair| pair[0].0 == pair[1].0) {
        return Err(fail(
            ErrorCode::DuplicateValue,
            format!("{} appears more than once in mcv", text(pair[0].0)),
        ));
    }
    if has_histogram && bounds.len() < 2 {
        return Err(fail(
            ErrorCode::InvalidHistogram,
            "a histogram needs at least 2 bounds",
        ));
    }
    if let Some(pair) = bounds.windows(2).find(|pair| pair[0] > pair[1]) {
        return Err(fail(
            ErrorCode::InvalidHistogram,
            format!(
                "bounds {} and {} are out of order",
                text(pair[0]),
                text(pair[1])
            ),
        ));
    }
    let values: BTreeSet<i128> = mcv.iter().map(|&(value, _)| value).collect();
    if let Some(&common) = bounds.iter().filter(|bound| values.contains(bound)).min() {
        return Err(fail(
            ErrorCode::InvalidHistogram,
            format!("the bound {} is also a common value", text(common)),
        ));
    }
    Ok(())
}

/// Check the JSON shape of section 1: the keys, and the JSON type of every
/// value. Return the input's object.
fn shape(data: &Value) -> Result<&Object, SpecError> {
    let Some(fields) = data.as_object() else {
        return Err(invalid_input("the input must be a JSON object"));
    };
    keys(fields, &REQUIRED, &OPTIONAL, "the input")?;
    expect(fields, "column", Kind::String)?;
    expect(fields, "type", Kind::String)?;
    expect(fields, "partitions", Kind::Integer)?;
    expect(fields, "null_rows", Kind::Integer)?;
    if fields.contains_key("scale") {
        expect(fields, "scale", Kind::Integer)?;
    }
    let Some(mcv) = fields.get("mcv").and_then(Value::as_array) else {
        return Err(invalid_input("mcv must be a list"));
    };
    for entry in mcv {
        let Some(entry) = entry.as_object() else {
            return Err(invalid_input("each mcv entry must be an object"));
        };
        keys(entry, &["value", "rows"], &[], "an mcv entry")?;
        expect(entry, "value", Kind::String)?;
        expect(entry, "rows", Kind::Integer)?;
    }
    match fields.get("histogram") {
        Some(Value::Object(histogram)) => {
            keys(histogram, &["bounds", "rows"], &[], "the histogram")?;
            let bounds = histogram.get("bounds").and_then(Value::as_array);
            if !bounds.is_some_and(|bounds| bounds.iter().all(|bound| bound.as_str().is_some())) {
                return Err(invalid_input("histogram bounds must be a list of strings"));
            }
            expect(histogram, "rows", Kind::Integer)?;
        }
        Some(Value::Null) => {}
        _ => return Err(invalid_input("histogram must be an object or null")),
    }
    Ok(fields)
}

/// Check that `object` has every required key and no key outside the two
/// lists, naming `what` in the message.
pub(crate) fn keys(
    object: &Object,
    required: &[&str],
    optional: &[&str],
    what: &str,
) -> Result<(), SpecError> {
    let missing: Vec<&str> = required
        .iter()
        .copied()
        .filter(|key| !object.contains_key(key))
        .collect();
    if !missing.is_empty() {
        return Err(invalid_input(format!(
            "{what} is missing {}",
            missing.join(", ")
        )));
    }
    let mut unknown: Vec<&str> = object
        .keys()
        .filter(|key| !required.contains(key) && !optional.contains(key))
        .collect();
    // Byte order is code point order for UTF-8, as Python sorts strings.
    unknown.sort_unstable();
    if !unknown.is_empty() {
        return Err(invalid_input(format!(
            "{what} has unknown fields: {}",
            unknown.join(", ")
        )));
    }
    Ok(())
}

/// The JSON type a field must have.
#[derive(Clone, Copy)]
pub(crate) enum Kind {
    String,
    Integer,
}

/// Check that the field `key` has the JSON type `kind`.
pub(crate) fn expect(object: &Object, key: &str, kind: Kind) -> Result<(), SpecError> {
    let value = object.get(key);
    let (ok, name) = match kind {
        Kind::String => (value.and_then(Value::as_str).is_some(), "string"),
        Kind::Integer => (value.and_then(Value::as_int).is_some(), "integer"),
    };
    if ok {
        Ok(())
    } else {
        Err(invalid_input(format!("{key} must be a JSON {name}")))
    }
}

pub(crate) fn invalid_input(message: impl Into<String>) -> SpecError {
    fail(ErrorCode::InvalidInput, message)
}

fn fail(code: ErrorCode, message: impl Into<String>) -> SpecError {
    SpecError::new(code, message)
}
