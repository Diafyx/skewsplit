//! skewsplit plans balanced JDBC partition predicates for Spark from a
//! column's distribution.
//!
//! This crate is the Rust port of the [Python package]'s pure core: no I/O
//! and no dependencies. Its API can still change before 1.0. The command line
//! is the `skewsplit` binary, which plans from a stats file and does not
//! connect to a database; reading PostgreSQL stays in the Python package.
//! The library has the value codecs ([`codecs`]), JSON as Python reads and
//! writes it ([`json`]), the planner input, read from JSON or built in code
//! ([`inputs`]), the range planner ([`planner`]) and the hash planner
//! ([`hashing`]) with their plans and the plans' reports and snippets
//! ([`plan`]), PostgreSQL's array text ([`pgarray`]), and the planner input
//! from PostgreSQL's statistics ([`pgstats`]) or from a sample of the column
//! ([`sample`]).
//!
//! Both implementations follow the same behaviour spec, `spec/SPEC.md` in the
//! repository, and pass the same golden fixtures.
//!
//! Build an input with [`inputs::InputBuilder`], or read it from JSON with
//! [`json::parse`], then plan it:
//!
//! ```
//! use skewsplit::{inputs::InputBuilder, json, planner};
//!
//! let input = InputBuilder::new("customer_id", "int4", 4)
//!     .null_rows(100)
//!     .common_value("42", 500)
//!     .histogram(["1", "250", "500", "750", "1000"], 400)
//!     .build()?;
//! let plan = planner::plan_input(&input);
//! assert_eq!(plan.predicates()[0], r#""customer_id" IS NULL"#);
//!
//! // The same input as JSON, as the command line reads it from a file.
//! let data = json::parse(
//!     r#"{"column": "customer_id", "type": "int4", "partitions": 4, "null_rows": 100,
//!         "mcv": [{"value": "42", "rows": 500}],
//!         "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400}}"#,
//! )?;
//! assert_eq!(planner::plan(&data)?, plan);
//! assert_eq!(skewsplit::SPEC_VERSION, "1.1");
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```
//!
//! [Python package]: https://pypi.org/project/skewsplit/

pub mod codecs;
mod decimal;
pub mod error;
pub mod hashing;
pub mod inputs;
pub mod json;
pub mod pgarray;
pub mod pgstats;
pub mod plan;
pub mod planner;
pub mod sample;

pub use error::{ErrorCode, SpecError};

/// The version of this crate.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The version of the behaviour spec this crate follows.
pub const SPEC_VERSION: &str = "1.1";
