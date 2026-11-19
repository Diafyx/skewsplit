# The spec and its fixtures

| Path | Contents |
|------|----------|
| [`SPEC.md`](SPEC.md) | The behaviour spec: input, ordinals, planning, predicates, output |
| [`schema/fixture.schema.json`](schema/fixture.schema.json) | JSON Schema of one fixture file |
| `fixtures/*.json` | The golden fixtures, one input and its expected output per file |

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

## Checks

`make check-fixtures` validates every fixture against the schema, then runs
`scripts/check_fixtures.py`, which checks what a schema cannot: the row
counts add up and match the input, the predicates fit together in order,
each warning is given exactly when the spec says, and the skew score follows
from the largest predicate. It does not plan, so a wrong split point is caught
by review of the description and by the implementations' tests: for Python,
`make conformance` runs every fixture against the core.

A change to `SPEC.md` that changes any output changes the fixtures in the same
pull request.
