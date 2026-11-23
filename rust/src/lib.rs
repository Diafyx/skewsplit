//! skewsplit plans balanced JDBC partition predicates for Spark from a
//! column's distribution.
//!
//! This crate is the Rust port of the [Python package], and is not usable
//! yet. The library will hold the pure core: no I/O and no dependencies. The
//! command line is the `skewsplit` binary. So far it has the value codecs
//! ([`codecs`]) and JSON as Python reads and writes it ([`json`]).
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
pub mod json;

pub use error::{ErrorCode, SpecError};

/// The version of this crate.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The version of the behaviour spec this crate follows.
pub const SPEC_VERSION: &str = "1.1";
