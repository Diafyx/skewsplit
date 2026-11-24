//! The `skewsplit` command line. `skewsplit plan --stats FILE` plans from a
//! column's distribution in a JSON file, as `skewsplit stats` of the Python
//! package writes it, and prints the plan as the Python command line does.

use std::ffi::{OsStr, OsString};
use std::io::{self, Read, Write};
use std::path::Path;
use std::process::ExitCode;

use skewsplit::json::{self, Value};
use skewsplit::planner::plan;

/// The output could not be written.
const FAILED: u8 = 1;
/// The command line is wrong, as in the Python command line.
const USAGE: u8 = 2;
/// The stats cannot be read or planned, as in the Python command line.
const INVALID: u8 = 3;

const USAGE_LINES: &str = "\
usage: skewsplit plan --stats FILE [--partitions N] [--format {text,json,pyspark,scala}]
       skewsplit --version";

const HELP: &str = "\
usage: skewsplit plan --stats FILE [--partitions N] [--format {text,json,pyspark,scala}]
       skewsplit --version

Plan balanced JDBC partition predicates for Spark from a column's
distribution, as `skewsplit stats` of the Python package writes it.

options:
  --stats FILE     the distribution as JSON (spec section 1), or - for standard
                   input
  --partitions N   the number of partitions; it replaces the file's partitions,
                   and is needed if the file has none
  --format FORMAT  text: the skew report (the default); json: the plan;
                   pyspark or scala: code that reads the table with the
                   predicates
  -h, --help       print this help
  --version        print the version

The output contains real values from the column: treat it like the data.

exit codes:
  0  success
  1  the output could not be written
  2  the command line is wrong
  3  the stats cannot be read, or the column cannot be planned";

/// What the plan is printed as.
#[derive(Clone, Copy)]
enum Format {
    Text,
    Json,
    PySpark,
    Scala,
}

/// What the command line asks for.
enum Command {
    Help,
    Version,
    Plan {
        stats: OsString,
        partitions: Option<i128>,
        format: Format,
    },
}

fn main() -> ExitCode {
    let command = match parse_args(std::env::args_os().skip(1)) {
        Ok(command) => command,
        Err(message) => {
            eprintln!("{USAGE_LINES}\nskewsplit: error: {message}");
            return ExitCode::from(USAGE);
        }
    };
    let output = match command {
        Command::Help => HELP.to_owned(),
        Command::Version => format!("skewsplit {}", skewsplit::VERSION),
        Command::Plan {
            stats,
            partitions,
            format,
        } => match run(&stats, partitions, format) {
            Ok(output) => output,
            Err(message) => {
                eprintln!("skewsplit: {message}");
                return ExitCode::from(INVALID);
            }
        },
    };
    let mut stdout = io::stdout().lock();
    match writeln!(stdout, "{output}").and_then(|()| stdout.flush()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("skewsplit: cannot write the output: {error}");
            ExitCode::from(FAILED)
        }
    }
}

/// Read the command line, or say what is wrong with it.
fn parse_args(args: impl IntoIterator<Item = OsString>) -> Result<Command, String> {
    let mut args = args.into_iter();
    let Some(first) = args.next() else {
        return Err("a command is needed: plan".to_owned());
    };
    match first.to_str() {
        Some("-h" | "--help") => Ok(Command::Help),
        Some("--version") => match args.next() {
            None => Ok(Command::Version),
            Some(arg) => Err(format!(
                "--version takes no arguments, not '{}'",
                arg.display()
            )),
        },
        Some("plan") => parse_plan(args),
        _ => Err(format!(
            "unknown command '{}': the command is plan",
            first.display()
        )),
    }
}

/// Read the options of `plan`. An option's value follows it, or `=`.
fn parse_plan(mut args: impl Iterator<Item = OsString>) -> Result<Command, String> {
    let mut stats = None;
    let mut partitions = None;
    let mut format = None;
    while let Some(arg) = args.next() {
        let Some(text) = arg.to_str() else {
            return Err(format!("unknown argument '{}'", arg.display()));
        };
        let (name, inline) = match text.split_once('=') {
            Some((name, value)) => (name, Some(OsString::from(value))),
            _ => (text, None),
        };
        if inline.is_none() && matches!(name, "-h" | "--help") {
            return Ok(Command::Help);
        }
        if !matches!(name, "--stats" | "--partitions" | "--format") {
            return Err(format!("unknown argument '{text}'"));
        }
        let value = match inline {
            Some(value) => value,
            None => match args.next() {
                Some(value) if !is_option(&value) => value,
                _ => return Err(format!("{name} needs a value")),
            },
        };
        let given = match name {
            "--stats" => stats.replace(value).is_some(),
            "--partitions" => partitions.replace(number(&value)?).is_some(),
            _ => format.replace(format_named(&value)?).is_some(),
        };
        if given {
            return Err(format!("{name} is given twice"));
        }
    }
    let Some(stats) = stats else {
        return Err("plan needs --stats FILE".to_owned());
    };
    Ok(Command::Plan {
        stats,
        partitions,
        format: format.unwrap_or(Format::Text),
    })
}

/// Whether an argument is an option rather than a value: it starts with `-`,
/// but isn't `-` itself, for standard input, or a negative number.
fn is_option(arg: &OsStr) -> bool {
    let text = arg.to_string_lossy();
    text.starts_with('-') && text != "-" && text.parse::<i128>().is_err()
}

fn number(value: &OsStr) -> Result<i128, String> {
    value
        .to_str()
        .and_then(|text| text.parse().ok())
        .ok_or_else(|| {
            format!(
                "--partitions needs a whole number, not '{}'",
                value.display()
            )
        })
}

fn format_named(value: &OsStr) -> Result<Format, String> {
    match value.to_str() {
        Some("text") => Ok(Format::Text),
        Some("json") => Ok(Format::Json),
        Some("pyspark") => Ok(Format::PySpark),
        Some("scala") => Ok(Format::Scala),
        _ => Err(format!(
            "--format is text, json, pyspark or scala, not '{}'",
            value.display()
        )),
    }
}

/// Plan from the stats and return the output, or the message of what failed.
fn run(stats: &OsStr, partitions: Option<i128>, format: Format) -> Result<String, String> {
    let (source, read) = if stats == "-" {
        let mut bytes = Vec::new();
        let read = io::stdin().read_to_end(&mut bytes).map(|_| bytes);
        ("the stats on standard input".to_owned(), read)
    } else {
        let path = Path::new(stats);
        (
            format!("the stats file {}", path.display()),
            std::fs::read(path),
        )
    };
    let bytes = read.map_err(|error| format!("cannot read {source}: {error}"))?;
    let text =
        String::from_utf8(bytes).map_err(|_| format!("{source} is not JSON: it is not UTF-8"))?;
    let mut data = json::parse(&text).map_err(|error| format!("{source} is not JSON: {error}"))?;
    if let (Some(partitions), Value::Object(object)) = (partitions, &mut data) {
        object.insert("partitions", Value::from(partitions));
    }
    let result = plan(&data).map_err(|error| format!("{}: {error}", error.code()))?;
    match format {
        Format::Text => Ok(result.report()),
        Format::Json => result
            .to_json(Some(2))
            .map_err(|error| format!("cannot write the plan as JSON: {error}")),
        Format::PySpark => Ok(result.to_pyspark()),
        Format::Scala => Ok(result.to_scala()),
    }
}
