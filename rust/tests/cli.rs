//! The `skewsplit` binary, run as a user runs it. `tests/fixtures.rs` checks
//! that its plans are the Python command line's, in every format.

use std::ffi::OsStr;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

const USAGE: &str = "\
usage: skewsplit plan --stats FILE [--partitions N] [--format {text,json,pyspark,scala}]
       skewsplit --version
";

/// The input of spec section 1, without its partitions.
const STATS: &str = r#"{"column": "customer_id", "type": "int4", "null_rows": 100,
  "mcv": [{"value": "7", "rows": 60}, {"value": "42", "rows": 500}],
  "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400}}"#;

const SUMMARY: &str = "customer_id (int4): 1060 estimated rows, 4 of 4 partitions plus one for NULL, skew score 1.887\n";

fn skewsplit<S: AsRef<OsStr>>(args: &[S], stdin: Stdio) -> Output {
    Command::new(env!("CARGO_BIN_EXE_skewsplit"))
        .args(args)
        .stdin(stdin)
        .output()
        .expect("the binary runs")
}

fn run<S: AsRef<OsStr>>(args: &[S]) -> Output {
    skewsplit(args, Stdio::null())
}

/// Run the binary with `input` on standard input.
fn run_with_input<S: AsRef<OsStr>>(args: &[S], input: &[u8]) -> Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_skewsplit"))
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the binary runs");
    child.stdin.take().unwrap().write_all(input).unwrap();
    child.wait_with_output().unwrap()
}

/// A file with these contents in the tests' own temporary folder.
fn file(name: &str, contents: &[u8]) -> PathBuf {
    let path = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    std::fs::write(&path, contents).unwrap();
    path
}

fn text(bytes: &[u8]) -> &str {
    std::str::from_utf8(bytes).unwrap()
}

/// The exit code and standard error of a failed run, which wrote nothing.
fn failure(output: &Output) -> (Option<i32>, &str) {
    assert_eq!(text(&output.stdout), "");
    (output.status.code(), text(&output.stderr))
}

/// The standard output of a run that succeeded and wrote no error.
fn success(output: &Output) -> &str {
    assert_eq!(text(&output.stderr), "");
    assert_eq!(output.status.code(), Some(0));
    text(&output.stdout)
}

#[test]
fn version_prints_the_crate_version() {
    let output = run(&["--version"]);
    assert_eq!(
        success(&output),
        format!("skewsplit {}\n", skewsplit::VERSION)
    );
}

#[test]
fn help_prints_the_options_and_exit_codes() {
    for args in [
        &["--help"][..],
        &["-h"],
        &["-h", "plan"],
        &["plan", "--help"],
        &["plan", "--stats", "stats.json", "-h"],
    ] {
        let output = run(args);
        let help = success(&output);
        assert!(help.starts_with(USAGE), "{args:?}");
        for line in [
            "\nThe output contains real values from the column: treat it like the data.\n",
            "\n  2  the command line is wrong\n",
            "\n  3  the stats cannot be read, or the column cannot be planned\n",
        ] {
            assert!(help.contains(line), "{line}");
        }
    }
}

#[test]
fn a_wrong_command_line_is_a_usage_error() {
    for (args, message) in [
        (&[][..], "a command is needed: plan"),
        (
            &["version"],
            "unknown command 'version': the command is plan",
        ),
        (
            &["--version", "--version"],
            "--version takes no arguments, not '--version'",
        ),
        (&["plan"], "plan needs --stats FILE"),
        (&["plan", "--partitions", "4"], "plan needs --stats FILE"),
        (&["plan", "--stats"], "--stats needs a value"),
        (
            &["plan", "--stats", "--format", "json"],
            "--stats needs a value",
        ),
        (
            &["plan", "--stats", "a", "--stats", "b"],
            "--stats is given twice",
        ),
        (
            &["plan", "--stats", "a", "--format", "json", "--format=text"],
            "--format is given twice",
        ),
        (
            &[
                "plan",
                "--stats",
                "a",
                "--partitions",
                "2",
                "--partitions",
                "3",
            ],
            "--partitions is given twice",
        ),
        (
            &["plan", "--stats", "a", "--partitions", "four"],
            "--partitions needs a whole number, not 'four'",
        ),
        (
            &["plan", "--stats", "a", "--partitions=1.5"],
            "--partitions needs a whole number, not '1.5'",
        ),
        (
            &["plan", "--stats", "a", "--format", "yaml"],
            "--format is text, json, pyspark or scala, not 'yaml'",
        ),
        (
            &["plan", "--stats", "a", "--bogus"],
            "unknown argument '--bogus'",
        ),
        (
            &["plan", "--stats", "a", "extra"],
            "unknown argument 'extra'",
        ),
        (&["plan", "--help=yes"], "unknown argument '--help=yes'"),
        (&["plan", "-s=a"], "unknown argument '-s=a'"),
    ] {
        let output = run(args);
        let expected = format!("{USAGE}skewsplit: error: {message}\n");
        assert_eq!(failure(&output), (Some(2), expected.as_str()), "{args:?}");
    }
}

#[cfg(unix)]
#[test]
fn an_argument_that_is_not_utf8_is_shown_with_a_replacement_character() {
    use std::os::unix::ffi::OsStrExt;

    let bad = OsStr::from_bytes(b"x\xff");
    for (args, message) in [
        (
            vec![bad],
            "unknown command 'x\u{fffd}': the command is plan",
        ),
        (
            vec![
                OsStr::new("plan"),
                OsStr::new("--stats"),
                OsStr::new("a"),
                bad,
            ],
            "unknown argument 'x\u{fffd}'",
        ),
        (
            vec![
                OsStr::new("plan"),
                OsStr::new("--stats=a"),
                OsStr::new("--format"),
                bad,
            ],
            "--format is text, json, pyspark or scala, not 'x\u{fffd}'",
        ),
        (
            vec![
                OsStr::new("plan"),
                OsStr::new("--stats=a"),
                OsStr::new("--partitions"),
                bad,
            ],
            "--partitions needs a whole number, not 'x\u{fffd}'",
        ),
    ] {
        let output = run(&args);
        let expected = format!("{USAGE}skewsplit: error: {message}\n");
        assert_eq!(failure(&output), (Some(2), expected.as_str()), "{args:?}");
    }
    // A file name need not be UTF-8.
    let output = run(&[OsStr::new("plan"), OsStr::new("--stats"), bad]);
    let error = std::fs::read(bad).unwrap_err();
    let expected = format!("skewsplit: cannot read the stats file x\u{fffd}: {error}\n");
    assert_eq!(failure(&output), (Some(3), expected.as_str()));
}

#[test]
fn plan_reads_the_stats_from_a_file_or_standard_input() {
    let path = file("section-1.json", STATS.as_bytes());
    let from_file = run(&[
        OsStr::new("plan"),
        OsStr::new("--stats"),
        path.as_os_str(),
        OsStr::new("--partitions"),
        OsStr::new("4"),
    ]);
    let report = success(&from_file);
    assert!(report.starts_with(SUMMARY), "{report}");
    let mut inline = OsStr::new("--stats=").to_owned();
    inline.push(&path);
    let output = run(&[
        OsStr::new("plan"),
        &inline,
        OsStr::new("--partitions=4"),
        OsStr::new("--format=text"),
    ]);
    assert_eq!(success(&output), report);
    let output = run_with_input(
        &["plan", "--partitions", "4", "--stats", "-"],
        STATS.as_bytes(),
    );
    assert_eq!(success(&output), report);
}

#[test]
fn partitions_come_from_the_option_or_the_file() {
    let with_partitions = STATS.replacen('{', r#"{"partitions": 4, "#, 1);
    let output = run_with_input(&["plan", "--stats", "-"], with_partitions.as_bytes());
    assert!(success(&output).starts_with(SUMMARY));
    // The option replaces the file's partitions.
    let output = run_with_input(
        &["plan", "--stats", "-", "--partitions", "2"],
        with_partitions.as_bytes(),
    );
    assert!(success(&output).contains(", 2 of 2 partitions plus one for NULL,"));
    let output = run_with_input(&["plan", "--stats", "-"], STATS.as_bytes());
    assert_eq!(
        failure(&output),
        (
            Some(3),
            "skewsplit: invalid_input: the input is missing partitions\n"
        )
    );
    // A negative number is a value, which the planner refuses.
    let output = run_with_input(
        &["plan", "--stats", "-", "--partitions", "-1"],
        STATS.as_bytes(),
    );
    let (code, stderr) = failure(&output);
    assert_eq!(code, Some(3));
    assert!(
        stderr.starts_with("skewsplit: invalid_partitions: "),
        "{stderr}"
    );
    // Stats that are not an object get no partitions, and the planner refuses them.
    let output = run_with_input(&["plan", "--stats", "-", "--partitions", "4"], b"[]");
    let (code, stderr) = failure(&output);
    assert_eq!(code, Some(3));
    assert!(stderr.starts_with("skewsplit: invalid_input: "), "{stderr}");
}

#[test]
fn stats_that_cannot_be_read_are_invalid() {
    let missing = Path::new(env!("CARGO_TARGET_TMPDIR")).join("missing.json");
    let not_utf8 = file("not-utf8.json", b"{\"column\": \"\xff\"}");
    let not_json = file("not-json.json", b"{\"column\": }");
    for (path, message) in [
        (
            missing.clone(),
            format!(
                "cannot read the stats file {}: {}",
                missing.display(),
                std::fs::read(&missing).unwrap_err()
            ),
        ),
        (
            not_utf8.clone(),
            format!(
                "the stats file {} is not JSON: it is not UTF-8",
                not_utf8.display()
            ),
        ),
        (
            not_json.clone(),
            format!(
                "the stats file {} is not JSON: Expecting value: line 1 column 12 (char 11)",
                not_json.display()
            ),
        ),
    ] {
        let output = run(&[OsStr::new("plan"), OsStr::new("--stats"), path.as_os_str()]);
        let expected = format!("skewsplit: {message}\n");
        assert_eq!(failure(&output), (Some(3), expected.as_str()));
    }
    let output = run_with_input(&["plan", "--stats", "-"], b"nope");
    assert_eq!(
        failure(&output),
        (
            Some(3),
            "skewsplit: the stats on standard input is not JSON: Expecting value: line 1 column 1 (char 0)\n"
        )
    );
}

#[test]
fn a_source_with_nan_has_no_json_plan() {
    let stats = STATS.replacen('{', r#"{"source": {"rows": NaN}, "#, 1);
    let output = run_with_input(
        &[
            "plan",
            "--stats",
            "-",
            "--partitions",
            "4",
            "--format",
            "json",
        ],
        stats.as_bytes(),
    );
    let (code, stderr) = failure(&output);
    assert_eq!(code, Some(3));
    assert!(
        stderr.starts_with("skewsplit: cannot write the plan as JSON: "),
        "{stderr}"
    );
    // The report doesn't show the source's numbers.
    let output = run_with_input(
        &["plan", "--stats", "-", "--partitions", "4"],
        stats.as_bytes(),
    );
    assert!(success(&output).starts_with(SUMMARY));
}

#[cfg(target_os = "linux")]
#[test]
fn a_standard_input_that_cannot_be_read_is_invalid() {
    // Reading a directory fails with EISDIR.
    let folder = std::fs::File::open(env!("CARGO_TARGET_TMPDIR")).unwrap();
    let output = skewsplit(&["plan", "--stats", "-"], Stdio::from(folder));
    assert_eq!(
        failure(&output),
        (
            Some(3),
            "skewsplit: cannot read the stats on standard input: Is a directory (os error 21)\n"
        )
    );
}

#[cfg(target_os = "linux")]
#[test]
fn output_that_cannot_be_written_fails() {
    // Writing to /dev/full fails with ENOSPC.
    let full = std::fs::File::options()
        .write(true)
        .open("/dev/full")
        .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_skewsplit"))
        .arg("--version")
        .stdout(Stdio::from(full))
        .output()
        .expect("the binary runs");
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(
        text(&output.stderr),
        "skewsplit: cannot write the output: No space left on device (os error 28)\n"
    );
}
