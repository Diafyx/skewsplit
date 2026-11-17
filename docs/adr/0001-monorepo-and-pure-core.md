# 1. Monorepo layout and a pure core with no dependencies

- Status: accepted
- Date: 2026-11-17

## Context

skewsplit plans JDBC partition predicates from a column's distribution. It will
ship as a Python package first and as a Rust crate with a CLI later. Both must
produce the same predicates for the same input, and the same behaviour has to
hold whether the distribution came from `pg_stats`, a sample or exact
quantiles.

The planning logic is pure computation: given counts, most common values and
histogram bounds, choose split points. Reading the database, rendering Spark
code and parsing command-line flags are separate concerns with their own
dependencies and failure modes.

Users run the package next to PySpark on platforms such as EMR and Glue, which
pin old Python versions and often restrict what can be installed.

## Decision

One repository holds every part, with one top-level folder per part:

| Folder | Contents |
|--------|----------|
| `spec/` | The behaviour spec and language-neutral golden fixtures (JSON). The shared truth for every implementation |
| `python/` | The `skewsplit` Python package, a member of the uv workspace defined by the root `pyproject.toml` |
| `rust/` | The Rust crate and CLI (later) |
| `bench/` | The CI benchmark against real PostgreSQL and Spark (later) |
| `docs/adr/` | Architecture decision records like this one |
| `scripts/` | Repository tooling used by the make targets and CI |

Inside the Python package:

- `skewsplit.core` is the planner. It imports only the standard library and
  modules inside `skewsplit.core`, and it does no I/O. A unit test parses
  every core module and fails on any other import.
- Adapters (for example `skewsplit.postgres`) and the CLI depend on the core.
  The core never depends on them.
- The package has no runtime dependencies. Database drivers and anything else
  an adapter needs become optional extras.

Each implementation is versioned and released on its own, with tags such as
`python-v0.1.0` and `rust-v0.1.0`.

## Consequences

- Golden fixtures in `spec/` run against both implementations, so a change of
  behaviour shows up as a fixture change in review.
- The core can be held to 100% line and branch coverage and to property and
  mutation tests without a database, and the same tests run on every supported
  Python and operating system.
- Installing skewsplit cannot conflict with a user's pinned packages, which
  keeps Python 3.9 support cheap.
- Shared files (`spec/`, CI, build files) change tests for every part, so CI
  must select jobs by path and run everything when a shared file changes.
- Contributors need uv for Python, and later a Rust toolchain for `rust/`.
