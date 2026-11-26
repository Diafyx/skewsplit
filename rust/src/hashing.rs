//! Hash predicates for a column without a usable order (spec section 16).
//!
//! Each predicate takes one remainder of a hash of the value's text, so the
//! plan needs only the row counts, works for any type, and gives every
//! partition about the same number of values. No index can serve these
//! predicates: each partition's query reads the whole table.
//!
//! ```
//! use skewsplit::{hashing::hash_plan, json};
//!
//! let data = json::parse(
//!     r#"{"column": "id", "type": "uuid", "partitions": 2, "null_rows": 0,
//!         "non_null_rows": 10}"#,
//! )?;
//! let result = hash_plan(&data)?;
//! assert_eq!(
//!     result.predicates()[1],
//!     r#"((pg_catalog.hashtextextended("id"::text, 0) % 2) + 2) % 2 = 0"#,
//! );
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```

use crate::codecs::INT64_MAX;
use crate::error::{ErrorCode, SpecError};
use crate::inputs::{self, Kind as Json, MAX_PARTITIONS, invalid_input};
use crate::json::{Int, Object, Value};
use crate::plan::{Kind, Plan, PlanWarning, Predicate, WarningCode};

const REQUIRED: [&str; 5] = ["column", "type", "partitions", "null_rows", "non_null_rows"];
const OPTIONAL: [&str; 1] = ["source"];

/// Check a decoded JSON input of spec section 16 and plan its hash predicates.
///
/// # Errors
///
/// If the input is invalid, with the code of spec section 16 for the first
/// problem found, in the order the Python core checks them.
pub fn hash_plan(data: &Value) -> Result<Plan, SpecError> {
    let fields = shape(data)?;
    let text = |key: &str| fields.get(key).and_then(Value::as_str).unwrap_or_default();
    // shape checked that these three are integers.
    let int = |key: &str| {
        fields
            .get(key)
            .and_then(Value::as_int)
            .map_or(Int::from(0), Clone::clone)
    };
    let (column, type_name) = (text("column"), text("type"));
    let (partitions, null_count, non_null_count) =
        (int("partitions"), int("null_rows"), int("non_null_rows"));
    if column.is_empty() || column.contains('\0') {
        return Err(fail(
            ErrorCode::InvalidColumn,
            "the column name must not be empty or contain U+0000",
        ));
    }
    if type_name.is_empty() || type_name.contains('\0') {
        return Err(fail(
            ErrorCode::UnsupportedType,
            "the type must not be empty or contain U+0000",
        ));
    }
    let n = partitions.saturating_i128();
    if !(1..=MAX_PARTITIONS).contains(&n) {
        return Err(fail(
            ErrorCode::InvalidPartitions,
            format!("partitions must be 1 to {MAX_PARTITIONS}, not {partitions}"),
        ));
    }
    if null_count.is_negative() || non_null_count.is_negative() {
        return Err(fail(
            ErrorCode::InvalidRows,
            "null_rows and non_null_rows must not be negative",
        ));
    }
    // Two counts that each fit in an i128 can still overflow when added, so
    // checked_add gives None then, instead of panicking or wrapping around.
    let fits = |a: i128, b: i128| a.checked_add(b).is_some_and(|total| total <= INT64_MAX);
    let (null_rows, non_null) = match (null_count.to_i128(), non_null_count.to_i128()) {
        (Some(null_rows), Some(non_null)) if fits(null_rows, non_null) => (null_rows, non_null),
        _ => {
            return Err(fail(
                ErrorCode::InvalidRows,
                format!(
                    "total_rows {} is above 2^63 - 1",
                    sum(&null_count.to_string(), &non_null_count.to_string())
                ),
            ));
        }
    };
    let total = null_rows + non_null;

    let quoted = format!("\"{}\"", column.replace('"', "\"\""));
    let remainder = format!("((pg_catalog.hashtextextended({quoted}::text, 0) % {n}) + {n}) % {n}");
    let mut parts = Vec::with_capacity(usize::try_from(n).unwrap_or_default() + 1);
    parts.push(Predicate {
        kind: Kind::Null,
        sql: format!("{quoted} IS NULL"),
        estimated_rows: null_rows,
    });
    // The rows of bucket i of section 3, with N buckets of R rows: they add up to R.
    parts.extend((0..n).map(|i| Predicate {
        kind: Kind::Hash(i),
        sql: format!("{remainder} = {i}"),
        estimated_rows: (i + 1) * non_null / n - i * non_null / n,
    }));
    let largest = parts
        .iter()
        .map(|part| part.estimated_rows)
        .max()
        .unwrap_or_default();
    Ok(Plan {
        column: column.to_owned(),
        type_name: type_name.to_owned(),
        scale: None,
        requested_partitions: n,
        null_rows,
        total_rows: total,
        parts,
        // Section 7: largest * N / total, rounded half up, in thousandths.
        skew_milli: (total > 0).then(|| (2 * largest * n * 1000 + total) / (2 * total)),
        warnings: warnings(n, null_rows, total),
        source: fields.get("source").cloned().unwrap_or(Value::Null),
    })
}

/// The warnings of spec section 16, in its order.
fn warnings(n: i128, null_rows: i128, total: i128) -> Vec<PlanWarning> {
    let mut warnings = vec![PlanWarning::new(
        WarningCode::FullScans,
        format!(
            "each of the {n} hash predicates reads the whole table: no index on the column can serve them, so the read scans the table once per partition"
        ),
    )];
    if null_rows * n > total {
        warnings.push(PlanWarning {
            rows: Some(null_rows),
            ..PlanWarning::new(
                WarningCode::NullsExceedShare,
                format!("{null_rows} NULL rows are more than one partition's share"),
            )
        });
    }
    if total == 0 {
        warnings.push(PlanWarning::new(
            WarningCode::NoRows,
            "the input shows no rows; the plan still reads rows added later",
        ));
    }
    warnings
}

/// Check the keys and the JSON type of every field of the input.
fn shape(data: &Value) -> Result<&Object, SpecError> {
    let Some(fields) = data.as_object() else {
        return Err(invalid_input("the input must be a JSON object"));
    };
    inputs::keys(fields, &REQUIRED, &OPTIONAL, "the input")?;
    for key in ["column", "type"] {
        inputs::expect(fields, key, Json::String)?;
    }
    for key in ["partitions", "null_rows", "non_null_rows"] {
        inputs::expect(fields, key, Json::Integer)?;
    }
    Ok(fields)
}

/// The sum of two non-negative integers in decimal, of any size, as the
/// message of a total beyond `i128` has to show it exactly, as Python does.
fn sum(a: &str, b: &str) -> String {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let mut digits = Vec::with_capacity(a.len().max(b.len()) + 1);
    let mut carry = 0;
    for k in 0..a.len().max(b.len()) {
        let digit = |x: &[u8]| x.len().checked_sub(k + 1).map_or(0, |at| x[at] - b'0');
        let d = digit(a) + digit(b) + carry;
        digits.push(b'0' + d % 10);
        carry = d / 10;
    }
    if carry > 0 {
        digits.push(b'0' + carry);
    }
    digits.iter().rev().map(|&d| char::from(d)).collect()
}

fn fail(code: ErrorCode, message: impl Into<String>) -> SpecError {
    SpecError::new(code, message)
}

#[cfg(test)]
mod tests {
    use super::sum;

    #[test]
    fn sum_adds_decimals_of_any_size() {
        for (a, b, total) in [
            ("0", "0", "0"),
            ("1", "9223372036854775807", "9223372036854775808"),
            ("999", "1", "1000"),
            ("5", "123", "128"),
            (
                "170141183460469231731687303715884105727",
                "170141183460469231731687303715884105727",
                "340282366920938463463374607431768211454",
            ),
        ] {
            assert_eq!(sum(a, b), total, "{a} + {b}");
            assert_eq!(sum(b, a), total, "{b} + {a}");
        }
    }
}
