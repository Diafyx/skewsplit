//! skewsplit plans balanced JDBC partition predicates for Spark from a
//! column's distribution.
//!
//! This crate is the Rust port of the [Python package], and is not usable
//! yet. The library will hold the pure core: no I/O and no dependencies. The
//! command line is the `skewsplit` binary, which plans from a stats file. So
//! far the library has the value codecs ([`codecs`]), JSON as Python reads
//! and writes it ([`json`]), the planner input checks ([`inputs`]), the
//! range planner ([`planner`]) and the hash planner ([`hashing`]) with their
//! plans and the plans' reports and snippets ([`plan`]), PostgreSQL's array
//! text ([`pgarray`]), and the planner input from a sample of the column
//! ([`sample`]).
//!
//! Both implementations follow the same behaviour spec, `spec/SPEC.md` in the
//! repository, and pass the same golden fixtures.
//!
//! ```
//! assert_eq!(skewsplit::SPEC_VERSION, "1.1");
//! ```
//!
//! [Python package]: https://pypi.org/project/skewsplit/

pub mod codecs;
pub mod error;
pub mod hashing;
pub mod inputs;
pub mod json;
pub mod pgarray;
pub mod plan;
pub mod planner;
pub mod sample;

pub use error::{ErrorCode, SpecError};

/// The version of this crate.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The version of the behaviour spec this crate follows.
pub const SPEC_VERSION: &str = "1.1";
