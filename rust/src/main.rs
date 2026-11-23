//! The `skewsplit` command line. For now it only reports its version.

use std::ffi::OsString;
use std::process::ExitCode;

/// The exit status of a wrong command line, as in the Python CLI.
const USAGE: u8 = 2;

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    if args == ["--version"] {
        println!("skewsplit {}", skewsplit::VERSION);
        ExitCode::SUCCESS
    } else {
        eprintln!("usage: skewsplit --version");
        ExitCode::from(USAGE)
    }
}
