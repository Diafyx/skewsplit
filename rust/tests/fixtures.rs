//! Every planner fixture in `spec/fixtures/`, byte for byte (spec section 10).
//! `make rust-conformance`, and the Rust leg of CI's conformance job, run
//! these tests.
//!
//! A fixture's `expected` leaves out the messages of warnings and errors,
//! whose wording is not part of the contract, so they are removed from the
//! plan before it is compared, after checking that each one is a non-empty
//! string.

use std::path::{Path, PathBuf};

use skewsplit::json::{self, Value};
use skewsplit::plan::error_value;
use skewsplit::planner::plan;

struct Fixture {
    name: String,
    input: Value,
    expected: Value,
}

/// The fixture folders this crate doesn't run yet, because it hasn't ported
/// what they test. A new folder fails
/// `every_fixture_folder_is_run_or_not_ported_yet` until it is run here or
/// listed.
const NOT_PORTED_YET: [&str; 4] = [
    "hash",     // the hash planner, spec section 16
    "pg-array", // reading array text, spec section 12: #36
    "pg-stats", // converting pg_stats, spec section 13
    "sample",   // converting a sample, spec section 14
];

fn fixture_folder() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../spec/fixtures")
}

fn fixtures() -> Vec<Fixture> {
    let mut paths: Vec<_> = std::fs::read_dir(fixture_folder())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .collect();
    paths.sort();
    paths
        .into_iter()
        .map(|path| {
            let mut fixture = json::parse(&std::fs::read_to_string(&path).unwrap()).unwrap();
            let Value::Object(object) = &mut fixture else {
                unreachable!("a fixture is an object");
            };
            Fixture {
                name: path.file_name().unwrap().to_string_lossy().into_owned(),
                input: object.remove("input").unwrap(),
                expected: object.remove("expected").unwrap(),
            }
        })
        .collect()
}

/// The value without the `message` of each warning and of the error, after
/// checking that each one is a non-empty string.
fn without_messages(mut value: Value) -> Value {
    let Value::Object(object) = &mut value else {
        return value;
    };
    if let Some(Value::Array(warnings)) = object.get("warnings").cloned() {
        let warnings = warnings
            .into_iter()
            .map(|mut warning| {
                if let Value::Object(fields) = &mut warning {
                    check_message(fields.remove("message").as_ref());
                }
                warning
            })
            .collect();
        object.insert("warnings", Value::Array(warnings));
    }
    if let Some(Value::Object(mut error)) = object.get("error").cloned() {
        check_message(error.remove("message").as_ref());
        object.insert("error", Value::Object(error));
    }
    value
}

fn check_message(message: Option<&Value>) {
    assert!(
        matches!(message, Some(Value::String(text)) if !text.is_empty()),
        "a message is a non-empty string, not {message:?}"
    );
}

#[test]
fn every_fixture_plans_byte_for_byte() {
    let fixtures = fixtures();
    assert!(fixtures.len() >= 40);
    let (mut plans, mut errors) = (0, 0);
    for fixture in fixtures {
        let actual = match plan(&fixture.input) {
            Ok(result) => {
                plans += 1;
                result.to_value()
            }
            Err(error) => {
                errors += 1;
                error_value(&error)
            }
        };
        let actual = without_messages(actual);
        for indent in [None, Some(2)] {
            assert_eq!(
                actual.to_json(indent).unwrap(),
                fixture.expected.to_json(indent).unwrap(),
                "{}",
                fixture.name
            );
        }
    }
    assert!(
        plans >= 15 && errors >= 15,
        "{plans} plans, {errors} errors"
    );
}

#[test]
fn every_fixture_folder_is_run_or_not_ported_yet() {
    let mut folders: Vec<String> = std::fs::read_dir(fixture_folder())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.is_dir())
        .map(|path| path.file_name().unwrap().to_string_lossy().into_owned())
        .collect();
    folders.sort();
    assert_eq!(folders, NOT_PORTED_YET);
}
