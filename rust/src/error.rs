//! The error the core returns for invalid input (spec sections 9, 12 and 13).
//!
//! The code is part of the contract and the message is not, so match on the
//! code in code.
//!
//! ```
//! use skewsplit::{ErrorCode, inputs::InputBuilder};
//!
//! let error = InputBuilder::new("id", "int4", 0).build().unwrap_err();
//! assert_eq!(error.code(), ErrorCode::InvalidPartitions);
//! assert_eq!(error.code().as_str(), "invalid_partitions");
//! ```

use std::fmt;

/// An error code from spec section 9. The codes are part of the contract.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ErrorCode {
    /// The input is not an object of the shape in section 1.
    InvalidInput,
    /// The column name is empty or contains U+0000.
    InvalidColumn,
    /// The type is not supported, or its scale is missing, extra or out of range.
    UnsupportedType,
    /// `partitions` is not between 1 and 10000.
    InvalidPartitions,
    /// A row count is out of its range.
    InvalidRows,
    /// A value is not in the text form of its type.
    InvalidValue,
    /// A value's ordinal is outside the type's range.
    ValueOutOfRange,
    /// Two `mcv` entries have the same value.
    DuplicateValue,
    /// The histogram bounds are too few, out of order, or in `mcv`.
    InvalidHistogram,
    /// Array text is not in its format, or has a NULL element.
    InvalidArray,
    /// A `pg_stats` row cannot be converted.
    InvalidStatistics,
}

impl ErrorCode {
    /// The code as the spec writes it, such as `invalid_value`.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::InvalidInput => "invalid_input",
            Self::InvalidColumn => "invalid_column",
            Self::UnsupportedType => "unsupported_type",
            Self::InvalidPartitions => "invalid_partitions",
            Self::InvalidRows => "invalid_rows",
            Self::InvalidValue => "invalid_value",
            Self::ValueOutOfRange => "value_out_of_range",
            Self::DuplicateValue => "duplicate_value",
            Self::InvalidHistogram => "invalid_histogram",
            Self::InvalidArray => "invalid_array",
            Self::InvalidStatistics => "invalid_statistics",
        }
    }
}

impl fmt::Display for ErrorCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Invalid planner input: a code from the spec, and a message for people,
/// whose wording is not part of the contract.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SpecError {
    code: ErrorCode,
    message: String,
}

impl SpecError {
    /// Create an error with a spec error code and a message.
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    /// The error code from spec section 9.
    #[must_use]
    pub fn code(&self) -> ErrorCode {
        self.code
    }

    /// The description for people.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for SpecError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for SpecError {}
