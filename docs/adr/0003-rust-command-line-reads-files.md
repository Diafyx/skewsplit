# 3. The Rust command line plans from files and does not connect to PostgreSQL

- Status: accepted
- Date: 2026-11-28

## Context

Since rust 0.2.0, the crate's library has the whole pure core of the
[spec](../../spec/SPEC.md): the range planner (sections 1 to 10), the hash
planner (section 16), PostgreSQL's array text (section 12), and the planner
input from PostgreSQL's statistics (section 13) or from a sample
(section 14). The `skewsplit` binary plans from a JSON file of section 1, as
`skewsplit stats` of the Python package writes it.

What is still only in Python needs a database connection: `skewsplit stats`
and `skewsplit verify`; reading the statistics, a sample and exact quantiles
(sections 13 to 15); choosing a strategy (section 17); and filtered reads
(section 18). This is `python/src/skewsplit/postgres/`, about 2,100 lines,
with psycopg as an optional extra.

[ADR 0001](0001-monorepo-and-pure-core.md) keeps the core free of
dependencies, with adapters that depend on the core and never the reverse.
skewsplit is run next to PySpark, on platforms such as EMR and Glue, so
Python is there wherever a read is planned.

We compared two options.

**A. The command line stays file based.** The Python package reads the
database; the Rust binary plans from its files.

- The crate keeps no dependencies, so its MSRV, its licences and its security
  advisories stay those of the standard library, and the static Linux
  binaries keep building with no C library or TLS stack.
- Every query, every transaction setting and verify mode stay in one
  implementation, which the PostgreSQL integration tests and the benchmark
  already cover.
- A user who has no Python cannot read the database with the binary alone.

**B. A PostgreSQL adapter for the binary.** A second crate in `rust/`, or a
Cargo feature of the binary only, so the library keeps no dependencies.

- It needs a driver, such as the `postgres` crate on `tokio-postgres`, and TLS
  for most servers: dozens of crates, with their own MSRV, licences and
  advisories to check in CI.
- The musl and Windows builds need a TLS stack that cross-compiles, and the
  release workflow and its provenance cover more code.
- The queries of sections 13 to 15 and 17 to 18 and verify mode would exist
  twice, and the Rust copy needs its own PostgreSQL integration tests in CI.

## Decision

Option A. The Rust command line plans from files and does not connect to
PostgreSQL.

- The library stays the pure core: no I/O and no dependencies.
- The binary reads files and standard input only. It may grow commands and
  options that read other inputs the core understands, such as the
  statistics of section 13 or a sample of section 14 as JSON, but it opens no
  network connection.
- Reading the database, choosing a strategy, filtered reads and verify mode
  stay in the Python package.
- The fixtures in `spec/` and `make differential` keep the two
  implementations giving the same plans for the same files.

## Consequences

- The next Rust milestones are about the library's API, its documentation
  and the command line's inputs, not about a driver.
- The crate's checks stay as they are. It has no runtime dependencies whose
  licences or security advisories CI must check, and CI runs no database for
  it.
- The binary's documentation shows the Python package's `skewsplit stats` as
  the way to write its input.
- A spec section that needs the database is implemented in Python only. A
  section that is pure computation is ported to the library, as sections 12
  to 14 and 16 were.
- If users ask for a single binary that reads the database, a new ADR revisits
  this. Option B, as a second crate in `rust/` that depends on the library, is
  where it would start, so the library does not change.
