//! Build the planner input from PostgreSQL's statistics (spec section 13).
//!
//! The input is one `pg_stats` row and its table's `reltuples`, as the texts
//! PostgreSQL prints. All arithmetic is exact, so every implementation gets
//! the same row counts from the same texts.
//!
//! ```
//! use skewsplit::{json, pgstats::from_pg_stats, planner::plan};
//!
//! let data = json::parse(
//!     r#"{"column": "id", "type": "int4", "partitions": 4, "reltuples": ["2000"],
//!         "null_frac": "0.05", "most_common_vals": "{42,7}",
//!         "most_common_freqs": "{0.25,0.03}", "histogram_bounds": "{1,250,500,750,1000}",
//!         "modified_since_analyze": 150}"#,
//! )?;
//! let input = from_pg_stats(&data)?;
//! assert_eq!(input.get("null_rows"), Some(&json::Value::from(100)));
//! assert_eq!(plan(&input)?.total_rows, 2000);
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```

use crate::codecs::{Codec, codec_for, quoted};
use crate::decimal::{Decimal, Natural};
use crate::error::{ErrorCode, SpecError};
use crate::inputs::{self, Kind, invalid_input, parse_input, scale_of};
use crate::json::{Int, Object, Value};
use crate::pgarray::parse_elements;

const REQUIRED: [&str; 9] = [
    "column",
    "type",
    "partitions",
    "reltuples",
    "null_frac",
    "most_common_vals",
    "most_common_freqs",
    "histogram_bounds",
    "modified_since_analyze",
];
const OPTIONAL: [&str; 1] = ["scale"];
const ARRAYS: [&str; 3] = ["most_common_vals", "most_common_freqs", "histogram_bounds"];
/// The longest number text. It keeps exact arithmetic cheap; PostgreSQL's
/// texts are far shorter.
const MAX_NUMBER_LENGTH: usize = 40;
/// More than a tenth of the rows changed: `autovacuum_analyze_scale_factor`.
const STALE_DIVISOR: u64 = 10;

/// Convert a decoded JSON `pg_stats` row and its table's `reltuples` into a
/// planner input (spec section 1), as a JSON object in the order of that
/// section.
///
/// # Errors
///
/// With the code of spec section 13, of section 12 for bad array text, or of
/// section 9 if the result is not a valid planner input, for the first
/// problem found, in the order the Python core checks them.
pub fn from_pg_stats(data: &Value) -> Result<Value, SpecError> {
    let fields = shape(data)?;
    let modified = fields.get("modified_since_analyze").and_then(Value::as_int);
    if modified.is_some_and(Int::is_negative) {
        return Err(fail("modified_since_analyze must not be negative"));
    }
    let text = |key: &str| fields.get(key).and_then(Value::as_str);
    let type_name = text("type").unwrap_or_default();
    let scale = fields.get("scale").and_then(Value::as_int);
    let codec = codec_for(type_name, scale_of(type_name, scale)?)?;
    let special = codec.special_values();
    let reltuples = fields
        .get("reltuples")
        .and_then(Value::as_array)
        .unwrap_or_default();
    if reltuples.is_empty() {
        return Err(fail("reltuples needs at least one row count"));
    }
    let mut total = Decimal::default();
    for text in reltuples {
        total = total.add(&number(text.as_str().unwrap_or_default(), "reltuples")?);
    }
    let rows_estimate = total.round();
    let null_frac = fraction(text("null_frac").unwrap_or_default(), "null_frac")?;
    let null_rows = null_frac.mul(&total).round();
    let common = common_values(text("most_common_vals"), text("most_common_freqs"), &total)?;
    let is_special = |value: &str| special.contains(&value);
    let mut unestimated = Natural::default();
    let mut mcv = Vec::with_capacity(common.len());
    for (value, rows) in &common {
        if is_special(value) {
            unestimated = unestimated.add(rows);
        } else {
            let mut entry = Object::new();
            entry.insert("value", Value::from(output(&codec, value)?));
            entry.insert("rows", rows.to_value());
            mcv.push(Value::Object(entry));
        }
    }
    let mut histogram = Value::Null;
    if let Some(bounds_text) = text("histogram_bounds") {
        let common_rows = common
            .iter()
            .fold(Natural::default(), |sum, (_, rows)| sum.add(rows));
        // max(0, a - b - c) is (a - b, or 0) - c, or 0, as b and c are at least 0.
        let rows = rows_estimate
            .saturating_sub(&null_rows)
            .saturating_sub(&common_rows);
        let (bounds, kept) = histogram_bounds(bounds_text, &rows, &is_special)?;
        unestimated = unestimated.add(&rows.saturating_sub(&kept));
        if bounds.len() >= 2 {
            let bounds = bounds
                .iter()
                .map(|bound| output(&codec, bound).map(Value::from))
                .collect::<Result<_, _>>()?;
            let mut object = Object::new();
            object.insert("bounds", Value::Array(bounds));
            object.insert("rows", kept.to_value());
            histogram = Value::Object(object);
        }
    }
    let field = |key: &str| fields.get(key).cloned().unwrap_or(Value::Null);
    let mut result = Object::new();
    result.insert("column", field("column"));
    result.insert("type", field("type"));
    if fields.contains_key("scale") {
        result.insert("scale", field("scale"));
    }
    result.insert("partitions", field("partitions"));
    result.insert("null_rows", null_rows.to_value());
    result.insert("mcv", Value::Array(mcv));
    result.insert("histogram", histogram);
    let mut source = Object::new();
    source.insert("statistics", Value::from("pg_stats"));
    source.insert("rows_estimate", rows_estimate.to_value());
    source.insert("unestimated_rows", unestimated.to_value());
    source.insert("modified_since_analyze", field("modified_since_analyze"));
    let warnings = warnings(&rows_estimate, &unestimated, modified);
    source.insert("warnings", Value::Array(warnings));
    result.insert("source", Value::Object(source));
    let result = Value::Object(result);
    parse_input(&result)?;
    Ok(result)
}

/// Each common value with its rows: at least 1, as section 1 needs.
fn common_values(
    values: Option<&str>,
    freqs: Option<&str>,
    total: &Decimal,
) -> Result<Vec<(String, Natural)>, SpecError> {
    let (values, freqs) = match (values, freqs) {
        (Some(values), Some(freqs)) => (parse_elements(values)?, parse_elements(freqs)?),
        (None, None) => return Ok(Vec::new()),
        _ => {
            return Err(fail(
                "most_common_vals and most_common_freqs must both be null or both be arrays",
            ));
        }
    };
    if values.len() != freqs.len() {
        return Err(fail(format!(
            "{} common values but {} frequencies",
            values.len(),
            freqs.len()
        )));
    }
    let one = Natural::from(1);
    values
        .into_iter()
        .zip(&freqs)
        .map(|(value, freq)| {
            let rows = fraction(freq, "a frequency")?.mul(total).round();
            Ok((value, rows.max(one.clone())))
        })
        .collect()
}

/// The bounds that are not special, and the rows of the buckets between them.
fn histogram_bounds(
    text: &str,
    rows: &Natural,
    is_special: &dyn Fn(&str) -> bool,
) -> Result<(Vec<String>, Natural), SpecError> {
    let bounds = parse_elements(text)?;
    if bounds.len() < 2 {
        return Err(SpecError::new(
            ErrorCode::InvalidHistogram,
            format!(
                "histogram_bounds has {} bounds; a histogram needs at least 2",
                bounds.len()
            ),
        ));
    }
    let ordinary = |bound: &String| !is_special(bound);
    let lead = bounds.iter().position(ordinary).unwrap_or(bounds.len());
    let trail = bounds[lead..]
        .iter()
        .rev()
        .position(ordinary)
        .unwrap_or(bounds.len() - lead);
    let kept = &bounds[lead..bounds.len() - trail];
    if kept.iter().any(|bound| is_special(bound)) {
        return Err(fail("special values can only be the first or last bounds"));
    }
    if kept.len() < 2 {
        return Ok((kept.to_vec(), Natural::default()));
    }
    // Buckets L + 1 to m - U, as section 3 counts them:
    // (m - U) × H / m - L × H / m. A count of bounds fits in a u64.
    let count = |n: usize| u64::try_from(n).unwrap_or(u64::MAX);
    let buckets = count(bounds.len() - 1);
    let below = |end: usize| rows.mul_small(count(end)).div_small(buckets);
    let kept_rows = below(bounds.len() - 1 - trail).saturating_sub(&below(lead));
    Ok((kept.to_vec(), kept_rows))
}

/// The warnings of spec section 13, in its order.
fn warnings(rows: &Natural, unestimated: &Natural, modified: Option<&Int>) -> Vec<Value> {
    let mut warnings = Vec::new();
    if let Some(modified) = modified
        && Natural::from_int(modified).mul_small(STALE_DIVISOR) > *rows
    {
        let mut warning = Object::new();
        warning.insert("code", Value::from("stale_statistics"));
        warning.insert("modified_rows", Value::Int(modified.clone()));
        warning.insert("rows", rows.to_value());
        warning.insert(
            "message",
            Value::from(format!(
                "{modified} rows changed since the statistics were gathered, more than a tenth of the {rows} rows; run ANALYZE for an accurate plan"
            )),
        );
        warnings.push(Value::Object(warning));
    }
    if *unestimated > Natural::default() {
        let mut warning = Object::new();
        warning.insert("code", Value::from("special_values_left_out"));
        warning.insert("rows", unestimated.to_value());
        warning.insert(
            "message",
            Value::from(format!(
                "{unestimated} rows at special values such as infinity, or in histogram buckets next to them, are left out of the estimates; the predicates still read them"
            )),
        );
        warnings.push(Value::Object(warning));
    }
    warnings
}

/// A value in the output form of section 2.
fn output(codec: &Codec, text: &str) -> Result<String, SpecError> {
    codec.text(codec.parse(text)?)
}

/// The exact value of a decimal text as PostgreSQL prints a `real`.
fn number(text: &str, what: &str) -> Result<Decimal, SpecError> {
    let length = text.chars().count();
    let decimal = if length <= MAX_NUMBER_LENGTH {
        Decimal::parse(text)
    } else {
        None
    };
    decimal.ok_or_else(|| {
        // Python shows the first 40 characters, in the quotes of its repr.
        let shown: String = text.chars().take(MAX_NUMBER_LENGTH).collect();
        fail(format!(
            "{what} {} is not a non-negative decimal number",
            quoted(&shown)
        ))
    })
}

/// A number that is at most 1.
fn fraction(text: &str, what: &str) -> Result<Decimal, SpecError> {
    let value = number(text, what)?;
    if value.is_above_one() {
        return Err(fail(format!("{what} {} is above 1", quoted(text))));
    }
    Ok(value)
}

/// Check the keys and the JSON type of every field this conversion reads.
fn shape(data: &Value) -> Result<&Object, SpecError> {
    let Some(fields) = data.as_object() else {
        return Err(invalid_input("the statistics must be a JSON object"));
    };
    inputs::keys_with(
        fields,
        &REQUIRED,
        &OPTIONAL,
        [
            "the statistics are missing",
            "the statistics have unknown fields",
        ],
    )?;
    inputs::expect(fields, "type", Kind::String)?;
    if fields.contains_key("scale") {
        inputs::expect(fields, "scale", Kind::Integer)?;
    }
    let reltuples = fields.get("reltuples").and_then(Value::as_array);
    if !reltuples.is_some_and(|texts| texts.iter().all(|text| text.as_str().is_some())) {
        return Err(invalid_input("reltuples must be a list of strings"));
    }
    inputs::expect(fields, "null_frac", Kind::String)?;
    for key in ARRAYS {
        if !matches!(fields.get(key), Some(Value::Null | Value::String(_))) {
            return Err(invalid_input(format!(
                "{key} must be a JSON string or null"
            )));
        }
    }
    if !matches!(
        fields.get("modified_since_analyze"),
        Some(Value::Null | Value::Int(_))
    ) {
        return Err(invalid_input(
            "modified_since_analyze must be a JSON integer or null",
        ));
    }
    Ok(fields)
}

fn fail(message: impl Into<String>) -> SpecError {
    SpecError::new(ErrorCode::InvalidStatistics, message)
}
