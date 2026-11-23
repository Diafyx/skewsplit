//! The `skewsplit` binary, run as a user runs it.

use std::process::{Command, Output};

fn skewsplit(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_skewsplit"))
        .args(args)
        .output()
        .expect("the binary runs")
}

#[test]
fn version_prints_the_crate_version() {
    let output = skewsplit(&["--version"]);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        format!("skewsplit {}\n", skewsplit::VERSION)
    );
    assert_eq!(String::from_utf8_lossy(&output.stderr), "");
}

#[test]
fn anything_else_is_a_usage_error() {
    for args in [
        &[][..],
        &["--help"],
        &["version"],
        &["--version", "--version"],
    ] {
        let output = skewsplit(args);
        assert_eq!(output.status.code(), Some(2), "{args:?}");
        assert_eq!(String::from_utf8_lossy(&output.stdout), "", "{args:?}");
        assert_eq!(
            String::from_utf8(output.stderr).unwrap(),
            "usage: skewsplit --version\n"
        );
    }
}
