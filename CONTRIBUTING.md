# Contributing to skewsplit

Thank you for helping. This file explains how work flows through the
repository. Most of these rules are checked by CI, and a failed check tells you
what to change.

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
Security problems go through [SECURITY.md](SECURITY.md), never a public issue.

## Principles

- Nothing reaches `main` except through a pull request.
- Every pull request starts from an issue. Only Dependabot and the release bot
  are exempt.
- Every issue belongs to a milestone, and every release is a closed milestone.
- CI is the source of truth. `make check` runs the same targets as CI, so the
  two cannot drift.
- When the project grows, CI grows in the same pull request: a new folder,
  tool or supported version updates `.github/filters.yml`, the CI matrices and
  the gate along with it.

## Setting up

You need [uv](https://docs.astral.sh/uv/) and `make`. Everything else for
Python, including Python itself, is installed by uv from `uv.lock`.

For the Rust crate in `rust/`, you also need [rustup](https://rustup.rs/),
which installs the toolchain that `rust/rust-toolchain.toml` names, and two
cargo tools:

```sh
cargo install cargo-nextest cargo-llvm-cov --locked
```

```sh
git clone https://github.com/Diafyx/skewsplit.git
cd skewsplit
make check
```

| Command | What it runs |
|---------|--------------|
| `make check` | Lint, unit and property tests, coverage gates, packaging and docs checks, for Python and Rust: everything a pull request runs that needs no database |
| `make check-all` | `make check` plus the lowest-dependency test run |
| `make lint` | ruff, ruff format, mypy, typos, actionlint, zizmor, the filter guard, and cargo fmt, clippy and rustdoc |
| `make py-unit` | Unit and property tests with coverage data |
| `make PYTHON=3.9 py-unit` | The same on another Python version |
| `make rust-test` | The crate's tests with cargo-nextest, and its doc tests |
| `make rust-test RUST_TOOLCHAIN=msrv` | The same with the minimum supported Rust version, once `rustup toolchain install` has installed it |
| `make rust-coverage` | The crate's coverage gates |
| `make rust-fuzz` | The array text reader's fuzz target, for `FUZZ_SECONDS` (60), with Rust nightly and cargo-fuzz |
| `make differential` | The Rust command line against the Python core on random inputs, after building the binary. `SEED=N` repeats a run |
| `make rust-mutation` | cargo-mutants on the crate's library, with cargo-nextest. `SHARD=k/n` tests one share of the mutants |
| `make rust-package` | The crate as crates.io would receive it: `cargo package`, a check of its files, then `cargo publish --dry-run`. `make packaging` runs it |

To run the same checks before each commit, install the hooks with
`uvx pre-commit install`. They use the tool versions pinned in `uv.lock`, and
cargo fmt from the Rust toolchain.

### Tests that run only in CI

The PostgreSQL integration tests, the Spark end-to-end tests and the benchmark
need a database server or Spark with Java. They run in GitHub Actions, in
service containers on GitHub's Linux runners. The tests are required checks, so
nothing reaches `main` untested. The benchmark (`bench.yml`, see
[bench/README.md](bench/README.md)) runs by hand and on pull requests that
change `bench/`, and never gates. `make py-integration`, `make spark-e2e` and
`make bench` exist so that CI and local runs use the same commands, but
locally they stop with a message unless you point them at a server you run
yourself.

`make py-integration` reads the server from `SKEWSPLIT_PG_DSN`, a libpq
connection string for a role that is a superuser, because the tests create a
role. CI runs them against every supported PostgreSQL version. They create two
schemas and a role with random names, and drop exactly those when they finish.
Use a throwaway database all the same.

## Testing standard

| Gate | Threshold |
|------|-----------|
| Line and branch coverage of the pure core (`skewsplit.core`) | 100% |
| Line and branch coverage of the whole package | At least 95% |
| Line coverage of the Rust library, the core of the crate | 100% |
| Line coverage of the whole crate | At least 95% |
| Coverage of the lines your pull request changes (`diff-cover`) | 100% |
| Warnings | Errors (`filterwarnings = error`, clippy and rustdoc with `-D warnings`) |
| Types | `mypy --strict`, no errors |
| Mutation score of the core and of the Rust library (nightly) | At least 90% of mutants killed |

- Tests live in `python/tests/<kind>/`. The folder sets the kind: `unit`,
  `property` (Hypothesis), `integration` (PostgreSQL), `spark`, or
  `differential` (the Rust binary, see `make differential`). A test outside
  these folders fails collection, so no test can be skipped by a missing
  marker. A plain `pytest` runs `unit` and `property`.
- Test order is random (`pytest-randomly`), so tests must not depend on each
  other.
- Rust tests live next to the code they test (`#[cfg(test)]` modules) or in
  `rust/tests/`. Like the Python core, the crate depends on the standard
  library only.
- An exclusion from coverage needs a reason on the same line:
  `# pragma: no cover -- only reached on Windows`. A bare `# pragma: no cover`
  excludes nothing.
- The core uses the standard library only and does no I/O. A test enforces
  this; see [ADR 0001](docs/adr/0001-monorepo-and-pure-core.md).
- `spec/fixtures/*.json` are the shared truth for every implementation. A
  change to the spec changes them in the same pull request, and
  `make check-fixtures` checks them; see [spec/README.md](spec/README.md).
- `make conformance` runs the fixtures against both implementations, and the
  `conformance` CI job runs each in a leg of its own:
  - `make py-conformance` runs every fixture against the Python core, plus the
    Hypothesis invariants of the spec (every value matches exactly one
    predicate, and so on), with the `ci` profile. `HYPOTHESIS_PROFILE` picks
    the profile: `dev` (default), `ci`, `nightly` or `fuzz`.
  - `make rust-conformance` runs the fixtures the crate has ported, in
    `rust/tests/fixtures.rs`. A fixture folder it hasn't ported yet must be
    listed there, so a new folder can't be skipped by accident. It also runs
    the invariants in `rust/tests/properties.rs` with proptest, with as many
    cases as `HYPOTHESIS_PROFILE` gives Python; `make rust-test` runs them with
    the `dev` count. When one fails, proptest saves its seed in
    `rust/tests/properties.proptest-regressions`: commit that file with the
    fix, and every later run replays the seed first.
- `make mutation` runs mutmut on the core, in about five minutes, and fails
  when fewer than 90% of the tested mutants are killed. A mutant that times
  out counts as not killed. The survivors are listed in
  `reports/mutation-survivors.txt`.
- `make rust-mutation` runs cargo-mutants on the crate's library, and fails
  under the same 90%. A mutant that does not build is not tested. The binary,
  `src/main.rs`, is left out: `tests/cli.rs` tests it. The tests read
  `../spec`, which cargo-mutants would not copy, so the target copies `rust/`
  and `spec/` to `reports/rust-mutation/tree` and mutates the copy: your files
  are never changed. The missed mutants are listed in
  `reports/rust-mutation/mutants.out/missed.txt`. A run takes about 30 minutes
  on 12 cores, and needs cargo-mutants and cargo-nextest, at the versions
  that `nightly.yml` pins (`cargo install --locked cargo-mutants@<version>`).
- **Nightly** (`nightly.yml`, every night and by hand) runs what is too slow
  for a pull request:
  - the whole CI suite, with Spark on every Python of each Spark line;
  - `make property conformance` with the `nightly` profile, and the property
    tests of each parser with `fuzz`;
  - `make differential` with the `nightly` profile and the run's id as `SEED`.
    It gives random inputs, most of them valid and some that break one rule,
    to the Python core and the Rust command line, and requires the same
    output in every format. When they differ, the smallest input is uploaded
    in the shape of a fixture, ready to become one;
  - `make mutation`, and `make rust-mutation` in 4 shards, which a last job
    scores together. When either fails, nightly opens its issue, or comments
    on the one already open. Run the workflow by hand with `dry-run` to
    rehearse those issues: it finds each issue and prints what it would write;
  - `make links`, which requests every external link in the docs.
- **Canary** (`canary.yml`, every Monday and by hand) runs the tests against
  releases that CI does not test yet, and never gates a pull request:
  - the next Python, pre-release or not, with `make py-unit`;
  - a PySpark line newer than the pinned ones, pre-release or not, with
    `make spark-e2e`;
  - a PostgreSQL beta or release candidate newer than CI's, with
    `make py-integration`;
  - a JDBC driver newer than the Makefile's, with `make spark-e2e`.

  `scripts/canary_versions.py` finds them, and a job whose release does not
  exist yet is skipped with a notice. When a job fails, the canary opens an
  issue, or comments on the one already open; `dry-run` rehearses it as in
  nightly.
- `make bench-core` times the planner. The `core-bench` job runs it when the
  core changes and reports the timings in its summary; it never gates.
- Every `>>>` example in a Markdown file is run by the docs check.
- A bug fix comes with a test that fails without the fix.
- **Flaky tests** are never retried silently. Label the issue `flaky`, open an
  issue, and quarantine the test behind a marker until it is fixed.

## Issues

- Open issues through the forms. Blank issues are disabled.
- Titles are short and imperative, for example `Add pg_stats histogram
  strategy`. A bug title describes the symptom, not the guessed cause.
- New issues get `status/triage`. A maintainer adds the type, area, size and
  priority labels and the milestone.
- An issue is done when its acceptance criteria are met, tests are added with
  the coverage gates passing, docs and README are updated where behaviour
  changed, and CI is updated if a component, folder, tool or supported version
  was added.

## Branches

- `main` is the only long-lived branch.
- Name branches `<type>/<issue-number>-<short-slug>`, for example
  `feat/12-pg-stats-strategy` or `fix/31-null-predicate`. The type matches the
  pull request title.
- Bring a branch up to date by merging `main` into it, not by rebasing.
- Branches are deleted after merge.

## Commits

- **Every commit is signed** (GPG or SSH). `main` rejects unsigned commits.
- Use Conventional Commit messages, as for pull request titles below.
- No attribution trailers (`Co-authored-by`, `Assisted-by`, `Generated-by`).
- Never commit secrets, `.env` files or local notes.
- `uv.lock` (and later `Cargo.lock`) are committed.

## Pull requests

- **Title**: `type(scope): summary`, for example
  `feat(core): give dominant values their own predicate`.
  - Types: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `ci`, `build`,
    `chore`, `revert`.
  - Scopes: the area names (`core`, `python`, `rust`, `postgres`, `spark`,
    `cli`, `spec`, `bench`, `ci`, `docs`, `release`) and `deps`.
  - A breaking change adds `!`: `feat(core)!: ...`.
  - The title becomes the squash commit, so it drives the changelog and the
    version bump.
- **Description**: use the template and keep every heading. Write "N/A" where
  a section does not apply. Link the issue with `Closes #N`.
- **Labels and milestone**: one `type/*` label matching the title type
  (`feat` → `type/feature`, `fix` → `type/bug` or `type/security`, the rest by
  name), at least one `area/*` label (including the one named by the scope),
  and the milestone of the linked issue.
- **One issue per pull request.** Up to three closely related small issues may
  share one.
- Merges are squash merges, done by a maintainer when every required check is
  green and every conversation is resolved.
- Dependabot opens weekly pull requests for GitHub Actions (`ci(deps)`) and
  for `uv.lock` (`build(deps)`), with minor and patch updates grouped. It
  never changes the version ranges in `pyproject.toml`; change those by hand.
  It also skips the test tools that have a separate range for each Python
  version, because it cannot update them. Update those with
  `uv lock --upgrade-package <name>`.

The `pr-hygiene` check enforces the title, branch, issue link, labels,
milestone and template headings, and lists anything to fix in its summary.

### Size

CI counts changed lines (added plus deleted), leaving out the generated files
marked `linguist-generated` in `.gitattributes`, and sets one label:

| Label | Lines changed | |
|-------|---------------|---|
| `size/S` | up to 100 | Fine; consider bundling with related work |
| `size/M` | 101 to 300 | Target |
| `size/L` | 301 to 700 | Target |
| `size/XL` | 701 to 1500 | Allowed |
| `size/XXL` | more than 1500 | Needs the "Why is this PR large?" section filled in, or CI fails |

### How CI selects jobs

`.github/filters.yml` sorts the changed files into groups, and only the jobs
those groups need run. Changes to `spec/`, `.github/`, `scripts/` or build
files run everything, and so does the `ci:full` label (re-run the workflow
after adding it). A guard fails if any tracked file matches no group, and the
single required check, `ci-gate`, passes only if every selected job passed.

## Labels

Labels are defined in `.github/labels.yml` and synced by a workflow; nobody
creates labels by hand.

| Group | Labels |
|-------|--------|
| Type | `type/feature`, `type/bug`, `type/perf`, `type/refactor`, `type/test`, `type/docs`, `type/ci`, `type/build`, `type/chore`, `type/security` |
| Area | `area/core`, `area/python`, `area/rust`, `area/postgres`, `area/spark`, `area/cli`, `area/spec`, `area/bench`, `area/ci`, `area/docs`, `area/release` |
| Size | `size/S`, `size/M`, `size/L`, `size/XL`, `size/XXL` |
| Priority | `priority/P0` (drop everything), `priority/P1` (this milestone), `priority/P2` (nice to have) |
| Status | `status/triage`, `status/blocked`, `status/needs-decision` |
| Other | `breaking-change`, `good first issue`, `help wanted`, `dependencies`, `flaky`, `ci:full`, `emergency-bypass` |

## Milestones, versions and releases

- One milestone per release: `python v0.1.0`, `python v0.2.0`, ...,
  `rust v0.1.0`, ... A release happens only when its milestone has no open
  issues.
- Semantic versioning. Before 1.0 a breaking change bumps the minor version;
  `feat` bumps the minor version, and `fix` and `perf` bump the patch version.
- Deprecated behaviour warns for one minor release and is removed no earlier
  than the next.
- Releases are automated with release-please. The Python package and the Rust
  crate have separate versions, changelogs and tags (`python-vX.Y.Z`,
  `rust-vX.Y.Z`). Each push to `main` updates a release pull request; merging
  it tags the release and publishes it after a maintainer approves.
- Merging the Python release pull request runs `release.yml`: the full CI
  suite, a build with the packaging checks, an SBOM and build provenance
  attestations, TestPyPI, an install test from TestPyPI on Linux, macOS and
  Windows, then PyPI after approval. The distributions, the SBOM and the
  attestations are attached to the GitHub Release, which stays a draft until
  then.
- Merging the Rust release pull request runs the same workflow: the full CI
  suite, `make rust-package` from a clean checkout, the command line built and
  run on Linux (x86_64 and arm64, linked statically with musl), macOS (Intel
  and Apple silicon) and Windows, and build provenance attestations for the
  binaries. Then crates.io after approval, in the `crates-io` environment. The
  binaries and their attestations are attached to the GitHub Release, which
  stays a draft until then.
- To rehearse a release, run the Release workflow from the Actions tab with
  "dry-run" on. It publishes a dev version to TestPyPI only, and runs the
  crate's jobs up to the attestations without publishing anything.
- The changelogs are generated from pull request titles. Do not edit them by
  hand; they are left out of the Markdown and spelling checks.

### Publishing the crate

crates.io trusts a workflow only for a crate that already exists, so the first
release publishes with an API token. Before it, an owner of the crate on
crates.io creates a token with the scopes `publish-new` and `publish-update`
for the crate `skewsplit` only, and an expiry date. It is saved as the secret
`CARGO_REGISTRY_TOKEN` of the `crates-io` environment, which needs a
maintainer's approval and accepts only `main`.

After the first release, switch to Trusted Publishing:

1. On crates.io, in the crate's settings, add a trusted publisher: GitHub, the
   repository `Diafyx/skewsplit`, the workflow `release.yml` and the
   environment `crates-io`.
2. Allow `rust-lang/crates-io-auth-action` in Settings > Actions > General, and
   add it to `ALLOWED` in `python/tests/unit/test_workflow_actions.py`.
3. In the `publish-crates-io` job of `release.yml`, grant `id-token: write` and
   add a step with `id: auth` before Publish that uses
   `rust-lang/crates-io-auth-action`, pinned to a commit SHA. Set
   `CARGO_REGISTRY_TOKEN` to `${{ steps.auth.outputs.token }}`, and remove the
   zizmor ignore on `cargo publish`.
4. After the next release has published that way, delete the secret
   `CARGO_REGISTRY_TOKEN` and revoke the token on crates.io.

## Supported versions

The table is in the [README](README.md#supported-versions). The policy:

- Support every Python version that a supported Spark line runs on, as long as
  the test tools still install on it.
- Support every PostgreSQL major version that the PostgreSQL project still
  supports.
- Drop a version only in a minor release, announced in the changelog one
  release ahead.
- A pull request that changes support updates the tables in `README.md` and
  `SECURITY.md` together. A test checks that they match.
