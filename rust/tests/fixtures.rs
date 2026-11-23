//! Every planner fixture in `spec/fixtures/`, byte for byte (spec section 10).
//!
//! A fixture's `expected` leaves out the messages of warnings and errors,
//! whose wording is not part of the contract, so they are removed from the
//! plan before it is compared.

use std::path::Path;

use skewsplit::json::{self, Value};
use skewsplit::plan::error_value;
use skewsplit::planner::plan;

struct Fixture {
    name: String,
    input: Value,
    expected: Value,
}

fn fixtures() -> Vec<Fixture> {
    let folder = Path::new(env!("CARGO_MANIFEST_DIR")).join("../spec/fixtures");
    let mut paths: Vec<_> = std::fs::read_dir(folder)
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

/// The value without the `message` of each warning and of the error.
fn without_messages(mut value: Value) -> Value {
    let Value::Object(object) = &mut value else {
        return value;
    };
    if let Some(Value::Array(warnings)) = object.get("warnings").cloned() {
        let warnings = warnings
            .into_iter()
            .map(|mut warning| {
                if let Value::Object(fields) = &mut warning {
                    fields.remove("message");
                }
                warning
            })
            .collect();
        object.insert("warnings", Value::Array(warnings));
    }
    if let Some(Value::Object(mut error)) = object.get("error").cloned() {
        error.remove("message");
        object.insert("error", Value::Object(error));
    }
    value
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
