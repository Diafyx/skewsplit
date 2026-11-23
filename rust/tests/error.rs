//! The error codes of spec section 9.

use skewsplit::{ErrorCode, SpecError};

#[test]
fn every_code_is_written_as_the_spec_writes_it() {
    let codes = [
        (ErrorCode::InvalidInput, "invalid_input"),
        (ErrorCode::InvalidColumn, "invalid_column"),
        (ErrorCode::UnsupportedType, "unsupported_type"),
        (ErrorCode::InvalidPartitions, "invalid_partitions"),
        (ErrorCode::InvalidRows, "invalid_rows"),
        (ErrorCode::InvalidValue, "invalid_value"),
        (ErrorCode::ValueOutOfRange, "value_out_of_range"),
        (ErrorCode::DuplicateValue, "duplicate_value"),
        (ErrorCode::InvalidHistogram, "invalid_histogram"),
        (ErrorCode::InvalidArray, "invalid_array"),
        (ErrorCode::InvalidStatistics, "invalid_statistics"),
    ];
    for (code, text) in codes {
        assert_eq!(code.as_str(), text);
        assert_eq!(code.to_string(), text);
    }
}

#[test]
fn an_error_has_a_code_and_a_message() {
    let error = SpecError::new(ErrorCode::InvalidRows, "rows must not be negative");
    assert_eq!(error.code(), ErrorCode::InvalidRows);
    assert_eq!(error.message(), "rows must not be negative");
    assert_eq!(error.to_string(), "rows must not be negative");
    let boxed: Box<dyn std::error::Error> = Box::new(error.clone());
    assert_eq!(boxed.to_string(), error.message());
}
