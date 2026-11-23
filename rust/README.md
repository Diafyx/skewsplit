# skewsplit (Rust)

The Rust port of [skewsplit](https://github.com/Diafyx/skewsplit), which plans
balanced, index-friendly JDBC partition predicates for Spark from a column's
distribution.

> **Status: not usable yet.** So far this crate has the range planner, which
> gives byte for byte the plans of the Python package, with the value codecs,
> the input checks and JSON as Python reads and writes it. The hash planner
> and the command line are being ported from the
> [Python package](https://pypi.org/project/skewsplit/), which you can use
> today.

Both implementations follow the same behaviour spec,
[`spec/SPEC.md`](https://github.com/Diafyx/skewsplit/blob/main/spec/SPEC.md),
and pass the same golden fixtures.

## Supported Rust versions

The minimum supported Rust version is 1.95. It is always a stable release at
least six months old, and is raised only in a minor release.

## Development

From the repository root, with [rustup](https://rustup.rs/),
[cargo-nextest](https://nexte.st/) and
[cargo-llvm-cov](https://github.com/taiki-e/cargo-llvm-cov) installed:

```sh
make rust-lint      # cargo fmt, clippy and rustdoc, with warnings as errors
make rust-test      # the tests with cargo-nextest, then the doc tests
make rust-coverage  # the tests with coverage, and the coverage gates
```

## License

Apache-2.0
