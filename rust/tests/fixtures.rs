//! Every planner fixture in `spec/fixtures/`, byte for byte (spec section 10),
//! every array text fixture in `spec/fixtures/pg-array/` (section 12), and
//! the plan of each planner input of the fixtures printed in each format of
//! the command line, as the Python command line prints it.
//! `make rust-conformance`, and the Rust leg of CI's conformance job, run
//! these tests.
//!
//! A fixture's `expected` leaves out the messages of warnings and errors,
//! whose wording is not part of the contract, so they are removed from the
//! plan before it is compared, after checking that each one is a non-empty
//! string.

use std::path::{Path, PathBuf};

use skewsplit::SPEC_VERSION;
use skewsplit::codecs::codec_for;
use skewsplit::error::SpecError;
use skewsplit::json::{self, Object, Value};
use skewsplit::pgarray::parse_values;
use skewsplit::plan::error_value;
use skewsplit::planner::plan;

struct Fixture {
    name: String,
    input: Value,
    expected: Value,
}

/// The fixture folders this file runs, besides the planner fixtures at the top.
const RUN: [&str; 1] = ["pg-array"];

/// The fixture folders this crate doesn't run yet, because it hasn't ported
/// what they test. A new folder fails
/// `every_fixture_folder_is_run_or_not_ported_yet` until it is run here or
/// listed.
const NOT_PORTED_YET: [&str; 3] = [
    "hash",     // the hash planner, spec section 16
    "pg-stats", // converting pg_stats, spec section 13
    "sample",   // converting a sample, spec section 14
];

fn fixture_folder() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../spec/fixtures")
}

/// The fixtures in a folder of `spec/fixtures/`, or at its top for `""`.
fn fixtures(folder: &str) -> Vec<Fixture> {
    let mut paths: Vec<_> = std::fs::read_dir(fixture_folder().join(folder))
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
    let fixtures = fixtures("");
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
    let mut known: Vec<&str> = RUN.iter().chain(&NOT_PORTED_YET).copied().collect();
    known.sort_unstable();
    assert_eq!(folders, known);
}

/// Read the text of a fixture's input as values of its type: the result of
/// spec section 12 as `{"spec_version": ..., "values": [...]}`, or an error.
fn read_array(input: &Value) -> Value {
    let read = || -> Result<Vec<String>, SpecError> {
        let type_name = input.get("type").and_then(Value::as_str).unwrap();
        let scale = input
            .get("scale")
            .and_then(Value::as_i128)
            .map(|scale| i64::try_from(scale).unwrap());
        let text = input.get("text").and_then(Value::as_str).unwrap();
        parse_values(text, &codec_for(type_name, scale)?)
    };
    match read() {
        Ok(values) => {
            let mut out = Object::new();
            out.insert("spec_version", Value::from(SPEC_VERSION));
            out.insert(
                "values",
                Value::Array(values.into_iter().map(Value::from).collect()),
            );
            Value::Object(out)
        }
        Err(error) => error_value(&error),
    }
}

#[test]
fn every_array_text_fixture_reads_byte_for_byte() {
    let fixtures = fixtures("pg-array");
    assert!(fixtures.len() >= 30);
    let (mut arrays, mut errors) = (0, 0);
    for fixture in fixtures {
        let actual = without_messages(read_array(&fixture.input));
        if actual.get("error").is_some() {
            errors += 1;
        } else {
            arrays += 1;
        }
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
        arrays >= 10 && errors >= 15,
        "{arrays} arrays, {errors} errors"
    );
}

/// What the Python command line prints for a plan in each `--format`.
fn printed(result: &skewsplit::plan::Plan, format: &str) -> String {
    match format {
        "text" => result.report(),
        "json" => result.to_json(Some(2)).unwrap(),
        "pyspark" => result.to_pyspark(),
        "scala" => result.to_scala(),
        _ => unreachable!("{format} is not a format"),
    }
}

/// `tests/cli/python-outputs.json` has what the Python command line prints
/// for each planner input of the fixtures, in each `--format`, and
/// `test_conformance.py` checks that it is up to date. The crate must give
/// the same, byte for byte, or the same error line.
#[test]
fn every_plan_is_printed_as_python_prints_it() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/cli/python-outputs.json");
    let file = json::parse(&std::fs::read_to_string(path).unwrap()).unwrap();
    let cases = file.get("cases").and_then(Value::as_array).unwrap();
    let names: Vec<&str> = cases
        .iter()
        .map(|case| case.get("name").and_then(Value::as_str).unwrap())
        .collect();
    for fixture in fixtures("") {
        assert!(names.contains(&fixture.name.as_str()), "{}", fixture.name);
    }
    let (mut plans, mut errors) = (0, 0);
    for (case, name) in cases.iter().zip(names) {
        match plan(case.get("input").unwrap()) {
            Ok(result) => {
                plans += 1;
                for format in ["text", "json", "pyspark", "scala"] {
                    let expected = case.get(format).and_then(Value::as_str).unwrap();
                    assert_eq!(printed(&result, format), expected, "{name} {format}");
                }
            }
            Err(error) => {
                errors += 1;
                let expected = case.get("error").and_then(Value::as_str).unwrap();
                assert_eq!(format!("{}: {error}", error.code()), expected, "{name}");
            }
        }
    }
    assert!(
        plans >= 50 && errors >= 20,
        "{plans} plans, {errors} errors"
    );
}
