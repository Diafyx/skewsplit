# Instructions for coding agents

skewsplit plans balanced JDBC partition predicates for Spark from a column's
distribution. The rules for humans are in [CONTRIBUTING.md](CONTRIBUTING.md);
everything there applies to agents too. This file is the short version.

## Layout

| Path | Contents |
|------|----------|
| `python/` | The `skewsplit` package (src layout) and its tests |
| `python/src/skewsplit/core/` | The pure planner: standard library only, no I/O |
| `spec/` | Behaviour spec and golden fixtures shared by every implementation |
| `bench/` | The Spark benchmark, run in CI by `bench.yml` |
| `rust/` | The Rust crate: the library is the pure core, the binary a CLI that plans from files and never connects to a database ([ADR 0003](docs/adr/0003-rust-command-line-reads-files.md)) |
| `scripts/` | Repository tooling used by make targets and CI (standard library only) |
| `docs/adr/` | Architecture decision records |

## Commands

```sh
make check       # everything a pull request runs that needs no database
make check-all   # plus the lowest-dependency test run
make lint        # ruff, ruff format, mypy, typos, actionlint, zizmor, filter guard, fixtures,
                 # cargo fmt, clippy and rustdoc
make py-unit     # unit and property tests with coverage data
make conformance # golden fixtures against Python and Rust, spec invariants, more Hypothesis examples
make mutation    # mutmut on the core (about five minutes; nightly runs it)
make rust-mutation  # cargo-mutants on the crate's library, on a copy (nightly runs it)
make rust-package   # cargo package, a check of the crate's files, cargo publish --dry-run
make rust-test   # the crate's tests (cargo-nextest) and doc tests
make rust-coverage  # the crate's coverage gates (cargo-llvm-cov)
make rust-fuzz   # the array text fuzz target (needs Rust nightly and cargo-fuzz)
make differential  # the Rust command line against Python on random inputs (nightly runs it)
uv run --frozen pytest python/tests/unit/test_version.py   # one file
```

Use `uv run --frozen`, so `uv.lock` never changes by accident. Change
dependencies only on purpose, with `uv lock`. The same goes for Cargo: the make
targets pass `--locked`.

PostgreSQL and Spark tests (`make py-integration`, `make spark-e2e`) and the
benchmark (`make bench`) run in CI only. Do not start Docker or install a database to run them.

## Rules that are easy to miss

- Run `make check` before you finish. Fix every failure; never lower a coverage
  threshold, skip a test or add a broad exclusion to get green.
- The core (`skewsplit.core`) has 100% line and branch coverage and imports
  only the standard library and itself.
- A coverage exclusion needs a reason: `# pragma: no cover -- <reason>`.
- Code must run on Python 3.9: use `from __future__ import annotations`, and no
  `match` statements or other syntax newer than 3.9.
- Rust code must build with the minimum supported version, `rust-version` in
  `rust/Cargo.toml`. The crate has no dependencies, only proptest as a
  dev-dependency for its tests, and clippy's pedantic lints are errors. Code
  that needs a database goes in the Python package, not the crate (ADR 0003).
  The fuzz targets in `rust/fuzz` are a crate of their own, with
  libfuzzer-sys.
- The package has no runtime dependencies.
- Tests go in `python/tests/<kind>/` (`unit`, `property`, `integration`,
  `spark`, `differential`); the folder sets the marker.
- A new folder, tool or supported version updates `.github/filters.yml`, the
  CI matrices and the gate in the same pull request.
- Commits use Conventional Commits, are signed, and carry no attribution
  trailers (`Co-authored-by`, `Assisted-by`, `Generated-by`).
- Pull requests follow the template, keep every heading and link an issue with
  `Closes #N`.
