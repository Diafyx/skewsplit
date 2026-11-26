# skewsplit (Rust)

The Rust port of [skewsplit](https://github.com/Diafyx/skewsplit), which plans
balanced, index-friendly JDBC partition predicates for Spark from a column's
distribution.

> **Status: not usable yet.** So far this crate has the range planner and
> the hash planner, which give byte for byte the plans of the Python package,
> with the value codecs, the input checks, JSON as Python reads and writes it,
> PostgreSQL's array text, and a command line that plans from a stats file.
> The conversions of PostgreSQL's statistics and of a sample are being ported
> from the [Python package](https://pypi.org/project/skewsplit/), which you
> can use today.

Both implementations follow the same behaviour spec,
[`spec/SPEC.md`](https://github.com/Diafyx/skewsplit/blob/main/spec/SPEC.md),
and pass the same golden fixtures.

## Install

Each release from 0.1.0 is on crates.io:

```sh
cargo install skewsplit --locked
```

The GitHub Release of each version (`rust-vX.Y.Z`) has the command line built
for Linux (x86_64 and arm64, with no dependencies), macOS (Intel and Apple
silicon) and Windows. Its build provenance shows that the repository's release
workflow built it:

```sh
gh attestation verify skewsplit-0.1.0-x86_64-unknown-linux-musl.tar.gz --repo Diafyx/skewsplit
```

## Command line

`skewsplit plan` plans from a column's distribution in a JSON file (spec
section 1), as `skewsplit stats` of the Python package writes it. It doesn't
connect to PostgreSQL:

```sh
skewsplit stats --table orders --column customer_id > stats.json  # the Python package
skewsplit plan --stats stats.json --partitions 8 --format pyspark
```

- `--stats FILE` is the file, or `-` for standard input.
- `--partitions N` replaces the file's `partitions`, and is needed if the file
  has none, as `skewsplit stats` writes it without `--partitions`.
- `--format` is `text`, the skew report (the default), `json`, the plan of
  spec section 10, or `pyspark` or `scala`, code that reads the table with the
  predicates. Each is what the Python package's `skewsplit plan` prints for
  the same distribution, byte for byte.

The exit code is 0 on success, 1 if the output could not be written, 2 for a
wrong command line, and 3 if the stats cannot be read or the column cannot be
planned. The output contains real values from the column: treat it like the
data.

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
`tests/properties.proptest-regressions`, to commit with the fix. Their inputs
are random, so the gate of 100% of the library's lines counts the other tests
only: a line that only a property test reaches needs a test of its own.

`tests/cli/python-outputs.json` holds what the Python command line prints for
the planner inputs of the fixtures, in each format, and `tests/fixtures.rs`
checks that this command line prints the same. After a change to the output,
write the file again from the repository root:

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
