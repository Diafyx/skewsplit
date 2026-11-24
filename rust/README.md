# skewsplit (Rust)

The Rust port of [skewsplit](https://github.com/Diafyx/skewsplit), which plans
balanced, index-friendly JDBC partition predicates for Spark from a column's
distribution.

> **Status: not usable yet.** So far this crate has the range planner, which
> gives byte for byte the plans of the Python package, with the value codecs,
> the input checks, JSON as Python reads and writes it, PostgreSQL's array
> text, and the PySpark and Scala snippets of a plan. The hash planner and the
> command line are being ported from the
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

A test that runs for more than 2 minutes is stopped and fails as `TIMEOUT`, and
a property test after 15 minutes. The limits are in `rust/.config/nextest.toml`.
They don't apply to the doc tests, which nextest doesn't run.

The property tests in `tests/properties.rs` run 100 cases each. Set
`PROPTEST_PROFILE` to `ci` for 1000, `nightly` for 20000 or `fuzz` for 100000.
A failing case is shrunk and printed, and its seed is saved in
`tests/properties.proptest-regressions`, to commit with the fix.

`tests/cli/python-outputs.json` holds what the Python command line prints for
the planner inputs of the fixtures, in each format, and `tests/fixtures.rs`
checks that this crate gives the same. After a change to the output, write the
file again from the repository root:

```sh
SKEWSPLIT_WRITE_CLI_OUTPUTS=1 uv run --frozen pytest python/tests/unit/test_conformance.py
```

The array text reader has a fuzz target in `fuzz/`, a crate of its own that
`make rust-lint` checks. Running it needs Rust nightly and
[cargo-fuzz](https://github.com/rust-fuzz/cargo-fuzz):

```sh
rustup toolchain install nightly --profile minimal
cargo install cargo-fuzz --locked
make rust-fuzz FUZZ_SECONDS=60   # from the repository root
```

A crashing input is saved in `fuzz/artifacts/pg_array/`. The nightly workflow
fuzzes for 10 minutes and uploads any crash.

## License

Apache-2.0
