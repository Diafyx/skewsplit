"""Plans from every row of the table, checked against the rows themselves.

No table here is analyzed: the exact strategy reads the table, not
``pg_stats``. Spec section 15 makes the NULL and value predicates exact and
every range predicate within ``2 x B`` rows of its estimate, so the actual
skew is within rounding of the estimated one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

from skewsplit.postgres.exact import exact_distribution, least_common_rows, plan_from_exact
from skewsplit.postgres.planning import plan
from skewsplit.postgres.verify import verify

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from integration.conftest import Database
    from skewsplit.core.plan import Plan

# Each table has one column, v, filled by its query, and is never analyzed.
# Every table has more than 10000 ordinary rows, so each has a histogram.
DISTRIBUTIONS = {
    "exact_uniform": ("integer", "SELECT g FROM generate_series(1, 50000) AS g"),
    "exact_dominant": (
        "bigint",
        "SELECT CASE WHEN g % 5 < 2 THEN 42 ELSE g * 1000 END FROM generate_series(1, 50000) AS g",
    ),
    "exact_null_heavy": (
        "integer",
        "SELECT CASE WHEN g % 10 < 6 THEN NULL ELSE g END FROM generate_series(1, 50000) AS g",
    ),
    "exact_gaps": (
        "integer",
        (
            "SELECT CASE WHEN g <= 20000 THEN g WHEN g <= 40000 THEN 1000000 + g"
            " ELSE 50000000 + g END FROM generate_series(1, 50000) AS g"
        ),
    ),
    # -50 to -1 with 1000 rows each, which are common, and 150000 distinct values.
    "exact_mixed": (
        "integer",
        (
            "SELECT CASE WHEN g % 4 = 0 THEN -1 - g / 4 % 50 ELSE g END"
            " FROM generate_series(1, 200000) AS g"
        ),
    ),
    "exact_amounts": (
        "numeric(12, 2)",
        "SELECT g * g / 100.0 FROM generate_series(1, 50000) AS g",
    ),
    "exact_dates": (
        "date",
        (
            "SELECT date '2000-01-01' + (g::bigint * g / 100000)::integer"
            " FROM generate_series(0, 49999) AS g"
        ),
    ),
    "exact_timestamptzs": (
        "timestamptz",
        (
            "SELECT timestamptz '2026-01-01 00:00:00+00' + g / 7 * interval '1 hour'"
            " + g % 7 * interval '1 microsecond' FROM generate_series(1, 50000) AS g"
        ),
    ),
    # A million rows in an order unrelated to their values, 5% NULL and 25% at 42.
    "exact_large": (
        "bigint",
        (
            "SELECT CASE WHEN g % 20 = 0 THEN NULL WHEN g % 10 < 3 THEN 42"
            " ELSE g::bigint * 7919 % 1000003 END FROM generate_series(1, 1000000) AS g"
        ),
    ),
}
# The tables with one row per value, so every split can be where the ideal one is.
EVEN = ("exact_uniform", "exact_gaps", "exact_amounts", "exact_timestamptzs")


def run(conn: psycopg.Connection[Any], template: str, **tables: str) -> None:
    """Run a statement with the test schema's tables filled in by name."""
    schema = conn.execute("SELECT current_schema()").fetchone()
    assert schema is not None
    names = {key: sql.Identifier(schema[0], name) for key, name in tables.items()}
    conn.execute(sql.SQL(template).format(**names))


def count(conn: psycopg.Connection[Any], table: str, where: str) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE {}")
    row = conn.execute(query.format(sql.Identifier(table), sql.SQL(where))).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def conn(database: Database) -> Iterator[psycopg.Connection[Any]]:
    """A superuser connection whose search_path is the test schema."""
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
        yield connection


@pytest.fixture(scope="module")
def distributions(database: Database) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
        for name, (sql_type, query) in DISTRIBUTIONS.items():
            run(
                admin,
                "CREATE TABLE {t} (v " + sql_type + ") WITH (autovacuum_enabled = false)",
                t=name,
            )
            run(admin, "INSERT INTO {t} (v) " + query, t=name)


def check_exact(conn: psycopg.Connection[Any], table: str, result: Plan) -> int:
    """Check spec section 15 on a table: exact counts, ranges within 2 x B; return B."""
    source: Any = result.source
    total = count(conn, table, "true")
    assert (source["statistics"], source["rows"], source["unestimated_rows"]) == ("exact", total, 0)
    data: Any = exact_distribution(conn, table, "v")
    histogram = data["histogram"]
    bucket = 0 if histogram is None else -(-histogram["rows"] // (len(histogram["bounds"]) - 1))
    found = verify(conn, table, "v", result.predicates, counts=True)
    assert found.ok
    assert found.total_rows == total == result.total_rows
    assert found.predicate_rows is not None
    for part, rows in zip(result.parts, found.predicate_rows):
        if part.kind == "range":
            assert abs(rows - part.estimated_rows) <= 2 * bucket, part.sql
        else:
            assert rows == part.estimated_rows, part.sql
    # So the skew scores differ by at most 2 x B x N / total_rows, and rounding.
    partitions = result.requested_partitions
    actual = found.skew_milli(partitions)
    assert actual is not None
    assert result.skew_milli is not None
    assert abs(actual - result.skew_milli) <= -(-2 * bucket * partitions * 1000 // total) + 1
    return bucket


@pytest.mark.usefixtures("distributions")
@pytest.mark.parametrize("partitions", [4, 16, 64])
@pytest.mark.parametrize("name", list(DISTRIBUTIONS))
def test_every_range_is_within_two_buckets_of_its_rows(
    conn: psycopg.Connection[Any], name: str, partitions: int
) -> None:
    result = plan_from_exact(conn, name, "v", partitions)
    bucket = check_exact(conn, name, result)
    data: Any = exact_distribution(conn, name, "v")
    assert data["histogram"] is not None
    # At most 10000 buckets, so B is at most a ten-thousandth of the rows, rounded up.
    assert bucket <= -(-data["histogram"]["rows"] // 10000)


@pytest.mark.usefixtures("distributions")
@pytest.mark.parametrize("partitions", [4, 16, 64])
@pytest.mark.parametrize("name", EVEN)
def test_the_actual_skew_is_within_rounding_of_ideal(
    conn: psycopg.Connection[Any], name: str, partitions: int
) -> None:
    result = plan_from_exact(conn, name, "v", partitions)
    found = verify(conn, name, "v", result.predicates, counts=True)
    actual = found.skew_milli(partitions)
    assert actual is not None
    assert found.predicate_rows is not None
    # The ideal is total / N rows each. B is 5 here, a range is off by at most
    # 2 x B, and the estimates themselves are off by a row or so.
    total = found.total_rows
    largest = -(-total // partitions) + 2 * 5 + 2
    assert max(found.predicate_rows) <= largest
    assert actual <= (2 * largest * partitions * 1000 + total) // (2 * total)


@pytest.mark.usefixtures("distributions")
def test_the_bounds_are_at_the_positions_of_the_spec(conn: psycopg.Connection[Any]) -> None:
    data: Any = exact_distribution(conn, "exact_mixed", "v")
    # The 50 values with 1000 rows each are common, the 150000 others are not.
    assert [entry["value"] for entry in data["mcv"]] == [str(v) for v in range(-50, 0)]
    assert {entry["rows"] for entry in data["mcv"]} == {1000}
    histogram = data["histogram"]
    h = histogram["rows"]
    assert (h, len(histogram["bounds"])) == (150000, 10001)
    least = least_common_rows(200000)
    for j in (0, 1, 2, 4999, 5000, 9999, 10000):
        position = j * (h - 1) // 10000
        row = conn.execute(
            "SELECT v::text FROM exact_mixed WHERE v NOT IN"
            " (SELECT v FROM exact_mixed GROUP BY v HAVING count(*) >= %s)"
            " ORDER BY v OFFSET %s LIMIT 1",
            (least, position),
        ).fetchone()
        assert row is not None
        assert histogram["bounds"][j] == row[0], j


@pytest.mark.usefixtures("distributions")
def test_the_dominant_value_gets_its_own_predicate(conn: psycopg.Connection[Any]) -> None:
    result = plan_from_exact(conn, "exact_large", "v", 8)
    assert [(w.code, w.value, w.rows) for w in result.warnings] == [
        ("dominant_value", "42", 250000)
    ]
    assert result.parts[0].estimated_rows == 50000
    assert '"v" = 42' in result.predicates


def test_a_table_with_few_rows_is_planned_from_its_values(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v integer) WITH (autovacuum_enabled = false)", t="exact_small")
    run(
        conn,
        "INSERT INTO {t} SELECT CASE WHEN g % 9 = 0 THEN NULL ELSE g % 37 END"
        " FROM generate_series(1, 5000) AS g",
        t="exact_small",
    )
    result = plan_from_exact(conn, "exact_small", "v", 6)
    data: Any = exact_distribution(conn, "exact_small", "v")
    # Fewer than 10000 rows: every value is common, so every estimate is exact.
    assert data["histogram"] is None
    assert check_exact(conn, "exact_small", result) == 0


def test_an_empty_table_has_no_rows(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v integer) WITH (autovacuum_enabled = false)", t="exact_empty")
    result = plan_from_exact(conn, "exact_empty", "v", 4)
    assert (result.total_rows, result.skew_milli) == (0, None)
    assert [w.code for w in result.warnings] == ["fewer_partitions", "no_rows"]
    assert verify(conn, "exact_empty", "v", result.predicates).ok


# A tenth of the rows at each of two special values, or two tenths at NaN.
SPECIAL = {
    "exact_special_date": (
        "date",
        (
            "INSERT INTO {t} SELECT CASE WHEN g % 10 = 0 THEN 'infinity'::date"
            " WHEN g % 10 = 1 THEN '-infinity'::date ELSE date '2000-01-01' + g END"
            " FROM generate_series(1, 30000) AS g"
        ),
    ),
    # A numeric column with a scale holds NaN, but not Infinity.
    "exact_special_numeric": (
        "numeric(10, 1)",
        (
            "INSERT INTO {t} SELECT CASE WHEN g % 10 < 2 THEN 'NaN'::numeric ELSE g / 10.0 END"
            " FROM generate_series(1, 30000) AS g"
        ),
    ),
    "exact_special_timestamp": (
        "timestamp",
        (
            "INSERT INTO {t} SELECT CASE WHEN g % 10 = 0 THEN '-infinity'::timestamp"
            " WHEN g % 10 = 1 THEN 'infinity'::timestamp"
            " ELSE timestamp '2000-01-01' + g * interval '1 minute' END"
            " FROM generate_series(1, 30000) AS g"
        ),
    ),
}


@pytest.mark.parametrize("table", list(SPECIAL))
def test_special_values_are_left_out_but_still_read(
    conn: psycopg.Connection[Any], table: str
) -> None:
    sql_type, insert = SPECIAL[table]
    run(conn, "CREATE TABLE {t} (v " + sql_type + ") WITH (autovacuum_enabled = false)", t=table)
    run(conn, insert, t=table)
    result = plan_from_exact(conn, table, "v", 4)
    source: Any = result.source
    assert source["unestimated_rows"] == 6000
    assert [w["code"] for w in source["warnings"]] == ["special_values_left_out"]
    assert (source["rows"], result.total_rows) == (30000, 24000)
    # The ranges at the ends read the special values too, so only the NULL
    # predicate's estimate is exact here.
    found = verify(conn, table, "v", result.predicates, counts=True)
    assert found.ok
    assert found.predicate_rows is not None
    assert found.predicate_rows[0] == result.parts[0].estimated_rows == 0


def test_a_partitioned_table_is_read_with_its_partitions(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {p} (v integer) PARTITION BY RANGE (v)", p="exact_parted")
    for name, bounds in (
        ("exact_parted_a", "(MINVALUE) TO (15000)"),
        ("exact_parted_b", "(15000) TO (MAXVALUE)"),
    ):
        run(
            conn,
            "CREATE TABLE {c} PARTITION OF {p} FOR VALUES FROM "
            + bounds
            + " WITH (autovacuum_enabled = false)",
            p="exact_parted",
            c=name,
        )
    run(conn, "INSERT INTO {p} SELECT g FROM generate_series(1, 30000) AS g", p="exact_parted")
    result = plan_from_exact(conn, "exact_parted", "v", 4)
    assert result.total_rows == 30000
    check_exact(conn, "exact_parted", result)


def test_the_callers_transaction_and_settings_are_left_as_they_were(
    conn: psycopg.Connection[Any],
) -> None:
    run(conn, "CREATE TABLE {t} (v timestamptz) WITH (autovacuum_enabled = false)", t="exact_tz")
    run(
        conn,
        "INSERT INTO {t} SELECT timestamptz '2026-01-01 00:00:00+00' + g * interval '1 minute'"
        " FROM generate_series(1, 20000) AS g",
        t="exact_tz",
    )
    with conn.transaction():
        conn.execute("SET LOCAL TimeZone = 'Asia/Kolkata'")
        conn.execute("SET LOCAL DateStyle = 'SQL, DMY'")
        result = plan_from_exact(conn, "exact_tz", "v", 4, statement_timeout_ms=60000)
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        settings = conn.execute(
            "SELECT current_setting('TimeZone'), current_setting('DateStyle'),"
            " current_setting('transaction_read_only'), current_setting('statement_timeout')"
        ).fetchone()
        assert settings == ("Asia/Kolkata", "SQL, DMY", "off", "0")
    assert result.parts[1].sql.endswith("+00'::timestamptz")
    check_exact(conn, "exact_tz", result)


@pytest.mark.usefixtures("distributions")
def test_the_strategy_reads_every_row_without_statistics(
    database: Database, record_testsuite_property: Callable[[str, object], None]
) -> None:
    table = f"{database.schema}.exact_large"
    result = plan(database.dsn, table, "v", 16, strategy="exact")
    source: Any = result.source
    assert (source["statistics"], source["rows"], result.total_rows) == ("exact", 10**6, 10**6)
    record_testsuite_property("million_rows_exact_planning_ms", source["planning_ms"])
    # Generous, for a shared runner.
    assert source["planning_ms"] < 60000


@pytest.mark.usefixtures("distributions")
def test_the_timeout_cancels_a_slow_scan(database: Database) -> None:
    table = f"{database.schema}.exact_large"
    with pytest.raises(psycopg.errors.QueryCanceled):
        plan(database.dsn, table, "v", 16, strategy="exact", statement_timeout_ms=1)
