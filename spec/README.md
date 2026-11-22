# The spec and its fixtures

| Path | Contents |
|------|----------|
| [`SPEC.md`](SPEC.md) | The behaviour spec: input, ordinals, planning, predicates, output |
| [`schema/fixture.schema.json`](schema/fixture.schema.json) | JSON Schema of one fixture file |
| `fixtures/*.json` | The golden fixtures, one input and its expected output per file |
| [`schema/pg-array.schema.json`](schema/pg-array.schema.json) | JSON Schema of one array text fixture |
| `fixtures/pg-array/*.json` | Cases for reading PostgreSQL array text (spec section 12) |
| [`schema/pg-stats.schema.json`](schema/pg-stats.schema.json) | JSON Schema of one statistics fixture |
| `fixtures/pg-stats/*.json` | Cases for converting PostgreSQL statistics into the input (spec section 13) |
| [`schema/sample.schema.json`](schema/sample.schema.json) | JSON Schema of one sample fixture |
| `fixtures/sample/*.json` | Cases for converting a sample of a column into the input (spec section 14) |

## Fixture format

Each fixture is one JSON object:

- `description`: how the expected output follows from the spec, with the
  arithmetic, so a reviewer can check it without running code.
- `input`: the planner input of spec section 1.
- `expected`: the plan of spec section 10, or an error of section 9.

The `message` of warnings and errors is left out, because its wording is not
part of the contract. An implementation passes a fixture when its output,
without the messages, equals `expected`: the same keys and values, with arrays
in the same order. Compare `skew_score` as a number.

Error fixtures have names starting with `error-`. Each has exactly one problem,
so only one error code is correct. Their input may be any JSON value, since
some test inputs of the wrong shape.

### Array text fixtures

The files in `fixtures/pg-array/` check spec section 12. Each has a
`description`, an `input` with the array `text`, the `type` and, for
`numeric`, the `scale`, and an `expected` result: `spec_version` with either
the `values` in order or an `error` code. They follow the same rules as the
planner fixtures: messages are left out, error fixtures are named `error-*`,
and each has exactly one problem.

### Statistics fixtures

The files in `fixtures/pg-stats/` check spec section 13. Each `input` is a
conversion input: one `pg_stats` row as text, with `reltuples` and the rows
modified since the last analyze. `expected` is `spec_version` with either the
planner `input` that the conversion builds, or an `error` code. The messages
of the warnings in the input's `source` are left out, like all messages, and
the same rules apply as for the other fixtures.

### Sample fixtures

The files in `fixtures/sample/` check spec section 14. Each `input` is a
conversion input: the table's estimated rows, and the sampled NULL rows and
values with their counts. `expected` is `spec_version` with either the planner
`input` that the conversion builds, or an `error` code, under the same rules
as the statistics fixtures.

## Checks

`make check-fixtures` validates every fixture against the schema, then runs
`scripts/check_fixtures.py`, which checks what a schema cannot: the row
counts add up and match the input, the predicates fit together in order,
each warning is given exactly when the spec says, and the skew score follows
from the largest predicate. For the array text fixtures it checks only the
names and the spec version; for the statistics and sample fixtures, also the
warnings in each result's `source`. It does not plan, so a wrong split point is
caught by review of the description and by the implementations' tests: for
Python, `make conformance` runs every fixture against the core.

A change to `SPEC.md` that changes any output changes the fixtures in the same
pull request.
