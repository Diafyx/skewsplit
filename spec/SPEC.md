# skewsplit behaviour spec

Spec version: **1.0**

This is the contract every skewsplit implementation follows. Given the same
input, every implementation produces the same output, byte for byte in each
value. The golden fixtures in `spec/fixtures/` check this. Why the algorithm
looks the way it does is in
[ADR 0002](../docs/adr/0002-split-algorithm.md).

The planner turns a summary of one column's distribution into SQL predicates,
one per Spark partition, for the `predicates` argument of Spark's JDBC reader.
It never talks to a database. Adapters, such as the PostgreSQL one, build the
input from database statistics.

Contents:

1. [Input](#1-input)
2. [Types and ordinals](#2-types-and-ordinals)
3. [The estimate model](#3-the-estimate-model)
4. [Planning](#4-planning)
5. [Predicates](#5-predicates)
6. [Every row exactly once](#6-every-row-exactly-once)
7. [Skew score](#7-skew-score)
8. [Warnings](#8-warnings)
9. [Errors](#9-errors)
10. [Output](#10-output)
11. [Versioning](#11-versioning)

In this document, `/` on integers is floor division, and every division is of
a non-negative number by a positive one. No step uses floating point.

## 1. Input

The input is one JSON object:

```json
{
  "column": "customer_id",
  "type": "int4",
  "partitions": 4,
  "null_rows": 100,
  "mcv": [
    {"value": "7", "rows": 60},
    {"value": "42", "rows": 500}
  ],
  "histogram": {"bounds": ["1", "250", "500", "750", "1000"], "rows": 400}
}
```

| Field | Meaning |
|-------|---------|
| `column` | The column name, unquoted. Not empty, and without the character U+0000 |
| `type` | One of the types in [section 2](#2-types-and-ordinals) |
| `scale` | For `numeric` only, and required there: the column's declared scale, 0 to 1000 |
| `partitions` | `N`, the most predicates the plan may have besides the NULL predicate. 1 to 10000 |
| `null_rows` | Rows where the column is NULL. At least 0 |
| `mcv` | The most common values, each with its row count (at least 1). Values are distinct; the order does not matter. May be empty |
| `histogram` | Either `null`, or the bounds of an equi-depth histogram over the rows not in `mcv`, and how many rows it covers (at least 0) |
| `source` | Optional. Any JSON value, copied to the output unchanged. Adapters use it to say where the estimate came from |

Values are strings in the type's text form, never JSON numbers, so that 64-bit
values survive every JSON parser.

The histogram has at least 2 bounds, in non-decreasing order. Equal
neighbouring bounds are allowed. No bound may equal a value in `mcv`; in
PostgreSQL's `pg_stats`, the most common values are left out of the
histogram, so this holds for its statistics.

The total row count is not an input. It is derived:

- `R`, the non-NULL rows, is the sum of the `mcv` rows plus the histogram rows
  (0 without a histogram).
- `total_rows` is `null_rows + R`. It must be at most 2^63 - 1.

**Example.** The input above describes 1060 rows: 100 NULL, 560 in two common
values and 400 spread over the histogram. `R` is 960.

## 2. Types and ordinals

Every supported type maps each value to an integer, its **ordinal**, so that
the order of ordinals is the order of the values. The planner works on
ordinals only. It reads values from their text form and writes them back to
text and to SQL literals.

| Type | Ordinal | Smallest ordinal | Largest ordinal |
|------|---------|------------------|-----------------|
| `int2` | The value | -32768 | 32767 |
| `int4` | The value | -2147483648 | 2147483647 |
| `int8` | The value | -9223372036854775808 | 9223372036854775807 |
| `numeric` | The value times 10^`scale` | -9223372036854775808 | 9223372036854775807 |
| `date` | Days since 1970-01-01 | -719162 (0001-01-01) | 2932896 (9999-12-31) |
| `timestamp` | Microseconds since 1970-01-01 00:00:00 | -62135596800000000 | 253402300799999999 |
| `timestamptz` | Microseconds since 1970-01-01 00:00:00 UTC | -62135596800000000 | 253402300799999999 |

Call these `MIN` and `MAX` for the input's type. A value whose ordinal falls
outside them is the error `value_out_of_range`.

Dates use the proleptic Gregorian calendar, as PostgreSQL does. The timestamp
limits are 0001-01-01 00:00:00 and 9999-12-31 23:59:59.999999.

### Text forms

Input values must be in exactly this form, which is how PostgreSQL prints
them with `DateStyle` set to ISO and `TimeZone` set to UTC. Anything else is
the error `invalid_value`. Output values use the same form.

| Type | Form | Examples |
|------|------|----------|
| `int2`, `int4`, `int8` | `-?(0\|[1-9][0-9]*)`; not `-0` | `0`, `-5`, `9223372036854775807` |
| `numeric` | `-?(0\|[1-9][0-9]*)(\.[0-9]+)?` with at most `scale` digits after the point; not a negative zero such as `-0.00`. Output has exactly `scale` digits after the point, and no point when `scale` is 0 | `12.5` reads as `12.50` when `scale` is 2 |
| `date` | `YYYY-MM-DD`, a real calendar date | `2026-11-18` |
| `timestamp` | `YYYY-MM-DD HH:MM:SS`, optionally followed by `.` and 1 to 6 digits. Hours 00 to 23, minutes and seconds 00 to 59. Output leaves out a zero fraction and trailing zeros of the fraction | `2026-11-18 15:30:00.25` |
| `timestamptz` | The `timestamp` form followed by `+00` | `2026-11-18 15:30:00.25+00` |

Infinite dates and timestamps, and `NaN` or infinite numerics, cannot appear in
the input. Section 6 explains why the predicates still cover such rows.

**Examples.**

- `date` `2026-11-18` has ordinal 20775.
- `timestamptz` `2026-11-18 15:30:00.25+00` has ordinal 1795015800250000.
- `numeric` with `scale` 2: `12.5` has ordinal 1250, and ordinal -7 is written
  `-0.07`.
- `numeric` with `scale` 2: `0.125` is `invalid_value` (three digits after the
  point).

### Why integers

With every quantity an integer and every division a floor division, two
implementations cannot differ by rounding. The largest intermediate product in
this spec is a row count (below 2^63) times an ordinal difference (below
2^64), which fits in a signed 128-bit integer.

## 3. The estimate model

The planner estimates, for any ordinal `x`, how many rows lie below it. The
rows come from three places:

- **NULL rows** are never below anything. They have their own predicate.
- **Most common values.** All rows of a value `v` sit exactly at `v`.
- **Histogram rows.** With bounds `b0 ≤ b1 ≤ ... ≤ bm` and `H` rows, bucket `i`
  (1 to `m`) runs from `b(i-1)` to `bi` and holds
  `h_i = i*H/m - (i-1)*H/m` rows. The buckets add up to exactly `H`.

Inside a bucket with `lo < hi`, rows are spread evenly, so the rows of bucket
`i` below `x` are:

| Where `x` is | Rows below `x` |
|--------------|----------------|
| `x ≤ lo` | 0 |
| `lo < x < hi` | `h_i * (x - lo) / (hi - lo)` |
| `x ≥ hi` | `h_i` |

A bucket with `lo = hi` holds all its rows at `lo`: 0 below `x` when `x ≤ lo`,
otherwise `h_i`.

Once the dominant values are chosen ([section 4.1](#41-dominant-values)),
define `G(x)` as the number of rows that are not NULL and not a dominant value,
and that lie below `x`: the rows of every other common value `v` with `v < x`,
plus the rows below `x` of every bucket. `G(-∞)` is 0, and `G(+∞)` is `R` minus
the rows of the dominant values. `G` never decreases as `x` grows.

**Example.** Bounds `1, 10, 20, 1000000` and `H` = 900 give three buckets of
300 rows. `G(8)` = 300 × (8 - 1) / 9 = 233, and `G(7)` = 300 × 6 / 9 = 200.
With `H` = 1000 and three buckets, the buckets hold 333, 333 and 334 rows.

## 4. Planning

### 4.1 Dominant values

A common value is a **candidate** if its rows are at least one partition's
share: `rows × N ≥ R`.

A plan with `D` dominant values needs `D` value predicates and at least `D + 1`
range predicates around them, so at most `(N - 1) / 2` values can be isolated.
Sort the candidates by rows, largest first, and smallest ordinal first for
equal rows. The
first `(N - 1) / 2` become **dominant values**, each with its own predicate and
the warning `dominant_value`. The rest stay in the ranges, each with the
warning `dominant_value_not_isolated`.

**Example.** In the input from section 1 (`N` = 4, `R` = 960), value 42 has
500 × 4 = 2000 ≥ 960 rows and becomes dominant. Value 7 has 60 × 4 = 240 and
does not. At most (4 - 1) / 2 = 1 value could be isolated.

**Example.** With `N` = 2, a value holding 700 of 1000 rows is a candidate,
but (2 - 1) / 2 = 0 values can be isolated, so it gets
`dominant_value_not_isolated` and stays inside a range.

### 4.2 Segments

Sorted by ordinal, the `D` dominant values `v1 < ... < vD` cut the non-NULL
domain into `D + 1` **segments**: below `v1`, between neighbours, and above
`vD`. With no dominant values there is one segment: everything. Segment `j` has
lower limit `L` (`-∞` or a dominant value) and upper limit `U` (a dominant
value or `+∞`), both excluded, and holds `m_j = G(U) - G(L)` estimated rows.

The plan has `N - D` range predicates to share among the segments. Each segment
gets one. The remaining `N - D - (D + 1)` go one at a time to the segment with
the largest `m_j / c_j`, where `c_j` is the number it already has. Compare
`m_a × c_b` with `m_b × c_a` to stay in integers. On a tie, the lowest segment
wins.

**Example.** Dominant values 1 and 44, `N` = 6, and segments holding 0, 88 and
212 rows. Four ranges for three segments leave one extra. 212 / 1 is the
largest share, so the counts are 1, 1 and 2.

### 4.3 Split points

A segment with `c` ranges needs `c - 1` split points. For `q` from 1 to
`c - 1`, the target is `t = q × m / c` rows above the segment's lower limit.
A target of 0 gives no split point.

For each target, find the candidate ordinals:

- `x_hi`, the smallest ordinal with `x_hi > L`, `x_hi ≥ MIN` and
  `G(x_hi) - G(L) ≥ t`. It always exists and is at most `MAX + 1`, because the
  segment holds more than `t` rows and none of them lie above `MAX`.
- `x_lo = x_hi - 1`. Then `G(x_lo) - G(L) < t`.

`x_hi` is a candidate if it is at most `MAX`; `x_lo` if it is above `L`. Of
the candidates, take the one whose `G(x) - G(L)` is closer to `t`; on a tie,
`x_hi`. At least one is a candidate: both fail only when `L = MAX`, and then
the segment holds no rows and has no targets.

Going through the targets in order, keep a split point `s` only if
`G(prev) < G(s) < G(U)`, where `prev` is the last kept split point of the
segment, or `L` at first. So every range inside a segment holds at least one
estimated row, and split points strictly increase.

**Example: nearest candidate.** Buckets of 300 rows over `1, 10, 20, 1000000`,
no common values, `N` = 4. The targets are 225, 450 and 675. For 225, `x_hi`
is 8 (`G` = 233) and `x_lo` is 7 (`G` = 200). 233 is 8 away from 225 and 200
is 25 away, so the split point is 8. The split points are 8, 15 and 250015.

**Example: a split point at the type limit.** An `int2` segment above a
dominant value 0 holds 80 rows: 20 histogram rows over `1, 100`, 30 rows at 50
and 30 rows at 32767. With `c` = 3, the targets are 26 and 53. For 53, `x_hi`
would be 32768, beyond `MAX`, so `x_lo` = 32767 is taken (`G` = 50). The ranges
are `> 0 AND < 51`, `>= 51 AND < 32767` and `>= 32767`.

**Example: a dropped split point.** `int4` bounds `1, 5, 5, 5, 100` with 400
rows give buckets of 100 rows, two of them all at 5. With `N` = 4, the targets
100, 200 and 300 pick 5, 5 and 6. The second 5 does not raise `G`, so it is
dropped. The plan has 3 ranges and the warning `fewer_partitions`.

### 4.4 Fewer partitions

The plan has `D` value predicates plus the range predicates that survive. When
that is less than `N`, the warning `fewer_partitions` says how many were
planned. It happens when the data has too few distinct values or too much of
it sits at single points.

**Example.** The dropped split point in section 4.3 leaves 3 ranges for
`N` = 4, so the plan warns `fewer_partitions` with `requested` 4 and
`planned` 3.

## 5. Predicates

The plan is a list of predicates. The first is always the NULL predicate. The
others follow in order of the values they match.

| Kind | SQL | Matches |
|------|-----|---------|
| `null` | `c IS NULL` | NULL |
| `value` | `c = v` | The dominant value `v` |
| `range` | `c < a` | Below the first boundary |
| `range` | `c >= a AND c < b` | From split point `a` up to `b` |
| `range` | `c > v AND c < b` | Just above dominant value `v`, up to `b` |
| `range` | `c >= a` or `c > v` | The last range |
| `range` | `c IS NOT NULL` | Everything non-NULL, when there is no boundary at all |

A range's lower end is a split point (`>=`), a dominant value (`>`) or open.
Its upper end is a split point or a dominant value (`<`), or open.

### Rendering

- The column is written in double quotes, with every `"` inside doubled:
  `customer_id` becomes `"customer_id"`, and `a"b` becomes `"a""b"`.
- Integers and numerics are written in their text form: `42`, `-5`, `12.50`.
- Dates and timestamps are quoted, with a cast:
  `'2026-11-18'::date`, `'2026-11-18 15:30:00'::timestamp`,
  `'2026-11-18 15:30:00+00'::timestamptz`.
- Single spaces separate the parts, and `AND` joins two comparisons. There
  are no parentheses. None are needed: no predicate contains `OR`, so a
  predicate can be combined with other conditions using `AND` as it is.

Every predicate compares the column directly, so a B-tree index on the column
can serve each partition's query.

**Example.** The input from section 1 plans:

| Predicate | Estimated rows |
|-----------|----------------|
| `"customer_id" IS NULL` | 100 |
| `"customer_id" < 42` | 76 |
| `"customer_id" = 42` | 500 |
| `"customer_id" > 42 AND "customer_id" < 520` | 192 |
| `"customer_id" >= 520` | 192 |

The segment below 42 holds value 7 (60 rows) and 16 histogram rows
(100 × 41 / 249). The segment above holds the remaining 384 rows and gets the
extra range, split at 520, where `G` reaches 76 + 192 = 268.

**Example.** With dominant value 0 as the smallest value, the range `< 0` has
0 estimated rows. It stays: a negative value added after the statistics were
taken would fall into it.

## 6. Every row exactly once

Take any row. Its column value is NULL or not.

- **NULL.** `c IS NULL` is true. Every other predicate compares the column
  with `=`, `<`, `>=` or `>`, or is `c IS NOT NULL`, and is not true for NULL,
  so a WHERE clause drops the row. The row is read once.
- **Not NULL.** The non-NULL predicates are built from a strictly increasing
  sequence of boundaries: split points and dominant values, all distinct.
  Split points are inside their segments and increase within a segment
  (section 4.3), and segments are separated by the dominant values in order.
  Write the boundaries `e1 < e2 < ... < ek`. The predicates then match, in
  order: values below `e1`; for each boundary that is a dominant value, the
  value itself; and between neighbouring boundaries, the values from `e(i)`
  (included if `e(i)` is a split point, excluded if it is a dominant value) up
  to `e(i+1)` (excluded); and finally values from `ek` upwards. Any value is
  either equal to some dominant value, and matched by its `=` predicate only,
  or lies in exactly one of these intervals. With no boundaries,
  `c IS NOT NULL` matches every value.

This uses only that the comparison operators order the column type totally,
which holds for every supported type in PostgreSQL. It does not depend on the
statistics: values that the statistics never saw, rows added after planning,
and PostgreSQL's special values (`infinity`, `-infinity`, and `NaN` for
`numeric`, which sorts above every number) all fall into exactly one
predicate. The estimates can be wrong; the coverage cannot.

**Example.** For the plan in section 5, value 41 matches only `< 42`, value 42
only `= 42`, value 43 only `> 42 AND < 520`, value 520 only `>= 520`, and NULL
only `IS NULL`.

Verify mode checks the same property against the real table by counting rows
that match no predicate or more than one.

## 7. Skew score

The skew score compares the largest predicate with an ideal split of all rows
into `N` partitions:

```text
skew score = (largest estimated rows of any predicate, the NULL predicate included) × N / total_rows
```

1.0 means perfectly even. It can be slightly below 1.0, because the NULL
predicate is one more partition than `N`. It is reported with three decimals,
rounded half up in integers:

```text
k = (2 × largest × N × 1000 + total_rows) / (2 × total_rows)
skew score = k / 1000
```

When `total_rows` is 0, the skew score is `null` and the warning `no_rows` is
given.

**Example.** For the plan in section 5: the largest predicate is `= 42` with
500 rows, so k = (2 × 500 × 4 × 1000 + 1060) / 2120 = 1887 and the skew score
is 1.887. The dominant value alone makes one partition almost twice the ideal
size.

## 8. Warnings

Warnings are listed in this order: `dominant_value` and then
`dominant_value_not_isolated`, each by ordinal, then `fewer_partitions`,
`nulls_exceed_share` and `no_rows`. Each warning has a `code`, the fields
below, and a `message` for people. The message wording is not part of the
contract.

| Code | Fields | When |
|------|--------|------|
| `dominant_value` | `value`, `rows` | A value got its own predicate. It holds at least one partition's share and this column cannot split it further |
| `dominant_value_not_isolated` | `value`, `rows` | A value holds at least one partition's share, but `N` is too small to give it its own predicate |
| `fewer_partitions` | `requested`, `planned` | The plan has fewer than `N` non-NULL predicates |
| `nulls_exceed_share` | `rows` | `null_rows × N > total_rows`: the NULL predicate holds more than one partition's share |
| `no_rows` | none | `total_rows` is 0 |

**Example.** A `date` column with 700 NULL rows out of 1000 and `N` = 3 gives
`nulls_exceed_share` with `rows` 700, since 700 × 3 > 1000. Its skew score is
2.1.

**Example.** An `int4` column with 10 NULL rows and nothing else, `N` = 3,
plans `"x" IS NULL` and `"x" IS NOT NULL`, with `fewer_partitions`
(`requested` 3, `planned` 1) and `nulls_exceed_share`.

## 9. Errors

Invalid input gives an error instead of a plan. An error has a `code` and a
`message`; the wording is not part of the contract. When an input has more
than one problem, which error is reported is not specified, so each error
fixture has exactly one.

| Code | When |
|------|------|
| `invalid_input` | The input is not an object of the shape in section 1: a field is missing, has the wrong JSON type, or is not known |
| `invalid_column` | The column name is empty or contains U+0000 |
| `unsupported_type` | The type is not in section 2; `scale` is missing for `numeric`, given for another type, or outside 0 to 1000 |
| `invalid_partitions` | `partitions` is not between 1 and 10000 |
| `invalid_rows` | A row count is negative, an `mcv` row count is 0, or `total_rows` exceeds 2^63 - 1 |
| `invalid_value` | A value is not in the text form of its type |
| `value_out_of_range` | A value's ordinal is outside the type's range |
| `duplicate_value` | Two `mcv` entries have the same value |
| `invalid_histogram` | Fewer than 2 bounds, bounds out of order, or a bound equal to a value in `mcv` |

**Example.** `"partitions": 0` gives `invalid_partitions`, and the value
`"2026-02-30"` for a `date` column gives `invalid_value`.

## 10. Output

A plan is one JSON object. For the input in section 1:

```json
{
  "spec_version": "1.0",
  "column": "customer_id",
  "type": "int4",
  "requested_partitions": 4,
  "planned_partitions": 4,
  "rows": {"total": 1060, "null": 100, "non_null": 960},
  "predicates": [
    {"kind": "null", "sql": "\"customer_id\" IS NULL", "estimated_rows": 100},
    {"kind": "range", "lower": null, "upper": {"op": "<", "value": "42"},
     "sql": "\"customer_id\" < 42", "estimated_rows": 76},
    {"kind": "value", "value": "42", "sql": "\"customer_id\" = 42", "estimated_rows": 500},
    {"kind": "range", "lower": {"op": ">", "value": "42"}, "upper": {"op": "<", "value": "520"},
     "sql": "\"customer_id\" > 42 AND \"customer_id\" < 520", "estimated_rows": 192},
    {"kind": "range", "lower": {"op": ">=", "value": "520"}, "upper": null,
     "sql": "\"customer_id\" >= 520", "estimated_rows": 192}
  ],
  "skew_score": 1.887,
  "warnings": [
    {"code": "dominant_value", "value": "42", "rows": 500,
     "message": "42 holds 500 rows, more than one partition's share; it cannot be split further on this column"}
  ],
  "source": null
}
```

- `scale` appears after `type` for `numeric` columns only.
- `planned_partitions` counts the predicates other than the NULL predicate.
- `estimated_rows` of a range is `G(upper) - G(lower)`, with `G` taken at the
  boundary values (`-∞` and `+∞` for open ends). A value predicate has the
  value's rows, and the NULL predicate has `null_rows`. The estimates add up
  to `total_rows`.
- `skew_score` is a JSON number equal to `k / 1000`, or `null`.
- `source` is the input's `source`, or `null` when the input has none.
- The order of keys in an object does not matter; the order of arrays does.
- An error is the object `{"spec_version": "1.0", "error": {"code": ..., "message": ...}}`.

## 11. Versioning

The spec version is `MAJOR.MINOR`. Each implementation states the version it
follows, and every plan carries it in `spec_version`.

- **Major**: anything that changes the output for an input that was valid
  before, or makes such an input an error.
- **Minor**: additions that leave every earlier output the same apart from new
  object keys, such as a new optional input field or a new type. Readers must
  ignore keys they do not know.

**Example.** Supporting a `uuid` column is a minor change: 1.0 to 1.1.
Rounding the targets in section 4.3 up instead of down changes existing plans,
so it is a major change: 1.1 to 2.0.

The fixtures in `spec/fixtures/` belong to the version in this file. A change
to the spec changes the fixtures in the same pull request. Until the first
release that implements version 1.0, version 1.0 itself may still change.
