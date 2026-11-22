"""Plans from a sample of the table, checked against the rows themselves.

No table here is analyzed, so ``pg_stats`` has nothing on them: the case the
sample strategy is for. A table no larger than the sample is read whole, and
spec section 14 bounds the error of its plan as section 13 does. Larger tables
are sampled with a seed, so a server version gives the same sample every run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

from skewsplit.postgres.planning import plan
from skewsplit.postgres.sample import plan_from_sample, sample_distribution
from skewsplit.postgres.verify import verify

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from integration.conftest import Database
    from skewsplit.core.plan import Plan

# Each table has one column, v, filled by its query, and is never analyzed.
DISTRIBUTIONS = {
    "sampled_uniform": ("integer", "SELECT g FROM generate_series(1, 30000) AS g"),
    "sampled_dominant": (
        "bigint",
        "SELECT CASE WHEN g % 5 < 2 THEN 42 ELSE g * 1000 END FROM generate_series(1, 30000) AS g",
    ),
    "sampled_null_heavy": (
        "integer",
        "SELECT CASE WHEN g % 10 < 6 THEN NULL ELSE g END FROM generate_series(1, 30000) AS g",
    ),
    "sampled_gaps": (
        "integer",
        (
            "SELECT CASE WHEN g <= 10000 THEN g WHEN g <= 20000 THEN 1000000 + g"
            " ELSE 50000000 + g END FROM generate_series(1, 30000) AS g"
        ),
    ),
    "sampled_amounts": (
        "numeric(12, 2)",
        "SELECT g * g / 100.0 FROM generate_series(1, 30000) AS g",
    ),
    "sampled_dates": (
        "date",
        "SELECT date '2000-01-01' + g * g / 100000 FROM generate_series(0, 29999) AS g",
    ),
    "sampled_timestamptzs": (
        "timestamptz",
        (
            "SELECT timestamptz '2026-01-01 00:00:00+00' + g / 7 * interval '1 hour'"
            " + g % 7 * interval '1 microsecond' FROM generate_series(1, 30000) AS g"
        ),
    ),
    # A million rows in an order unrelated to their values, 5% NULL and 25% at 42.
    "sampled_large": (
        "bigint",
        (
            "SELECT CASE WHEN g % 20 = 0 THEN NULL WHEN g % 10 < 3 THEN 42"
            " ELSE g::bigint * 7919 % 1000003 END FROM generate_series(1, 1000000) AS g"
        ),
    ),
}
# Larger than every table above but the last, so they are read whole.
WHOLE = 100000


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


def check_whole(conn: psycopg.Connection[Any], table: str, result: Plan) -> None:
    """Check spec section 14 for a table read whole: exact counts, ranges within 2 x B."""
    source: Any = result.source
    assert (source["method"], source["percent"], source["seed"]) == ("all", None, None)
    total = count(conn, table, "true")
    assert source["rows_estimate"] == source["sample_rows"] == total == result.total_rows
    data: Any = sample_distribution(conn, table, "v", sample_rows=WHOLE)
    histogram = data["histogram"]
    bucket = 0 if histogram is None else -(-histogram["rows"] // (len(histogram["bounds"]) - 1))
    actual = [count(conn, table, part.sql) for part in result.parts]
    assert sum(actual) == total
    for part, rows in zip(result.parts, actual):
        if part.kind == "range":
            assert abs(rows - part.estimated_rows) <= 2 * bucket, part.sql
        else:
            assert rows == part.estimated_rows, part.sql


@pytest.mark.usefixtures("distributions")
@pytest.mark.parametrize("partitions", [4, 16])
@pytest.mark.parametrize("name", [name for name in DISTRIBUTIONS if name != "sampled_large"])
def test_a_table_no_larger_than_the_sample_is_read_whole(
    conn: psycopg.Connection[Any], name: str, partitions: int
) -> None:
    result = plan_from_sample(conn, name, "v", partitions, sample_rows=WHOLE, seed=5)
    check_whole(conn, name, result)


@pytest.mark.usefixtures("distributions")
def test_the_dominant_value_of_a_sample_gets_its_own_predicate(
    conn: psycopg.Connection[Any],
) -> None:
    result = plan_from_sample(conn, "sampled_dominant", "v", 8, sample_rows=WHOLE)
    assert [(w.code, w.value, w.rows) for w in result.warnings] == [("dominant_value", "42", 12000)]
    assert result.parts[1].sql == '"v" < 42'


def check_sampled(conn: psycopg.Connection[Any], result: Plan, method: str) -> dict[str, Any]:
    """Check a plan from a sample of sampled_large against its rows."""
    source: dict[str, Any] = result.source  # type: ignore[assignment]
    assert source["method"] == method
    assert source["warnings"] == []
    # The estimate of the table's rows follows from the sample size.
    assert 700000 <= source["rows_estimate"] <= 1300000
    found = verify(conn, "sampled_large", "v", result.predicates, counts=True)
    assert found.ok
    assert found.total_rows == 1000000
    assert '"v" = 42' in result.predicates
    # 42 holds a quarter of the rows, so it sets the skew score: about 2.0 for 8.
    actual = found.skew_milli(8)
    assert actual is not None
    assert result.skew_milli is not None
    assert abs(actual - result.skew_milli) <= 250
    return source


@pytest.mark.usefixtures("distributions")
def test_a_large_table_is_sampled_by_row(conn: psycopg.Connection[Any]) -> None:
    # The planner estimates about a million rows, so 200000 is about 20%.
    result = plan_from_sample(conn, "sampled_large", "v", 8, sample_rows=200000, seed=1)
    source = check_sampled(conn, result, "bernoulli")
    assert 100000 <= source["sample_rows"] <= 300000


@pytest.mark.usefixtures("distributions")
def test_a_large_table_is_sampled_by_page(conn: psycopg.Connection[Any]) -> None:
    result = plan_from_sample(conn, "sampled_large", "v", 8, sample_rows=50000, seed=1)
    source = check_sampled(conn, result, "system")
    assert 10000 <= source["sample_rows"] <= 150000
    assert source["percent"] < 10


@pytest.mark.usefixtures("distributions")
@pytest.mark.parametrize("sample_rows", [50000, 200000])
def test_the_same_seed_gives_the_same_sample(
    conn: psycopg.Connection[Any], sample_rows: int
) -> None:
    def sampled() -> dict[str, object]:
        return sample_distribution(conn, "sampled_large", "v", sample_rows=sample_rows, seed=11)

    assert sampled() == sampled()


def test_the_limit_stops_the_sample(conn: psycopg.Connection[Any]) -> None:
    # Without statistics, the planner takes a text value to be 32 bytes wide,
    # so it estimates about 22000 of these 40000 rows with an empty text: the
    # table is read whole, until the limit stops it.
    run(
        conn,
        "CREATE TABLE {t} (v integer, pad text) WITH (autovacuum_enabled = false)",
        t="sampled_narrow",
    )
    run(
        conn, "INSERT INTO {t} SELECT g, '' FROM generate_series(1, 40000) AS g", t="sampled_narrow"
    )
    result = plan_from_sample(
        conn, "sampled_narrow", "v", 4, sample_rows=25000, max_sample_rows=25000
    )
    source: Any = result.source
    assert (source["method"], source["sample_rows"], source["sample_limit"]) == (
        "all",
        25000,
        25000,
    )
    assert [w["code"] for w in source["warnings"]] == ["sample_limit_reached"]
    assert source["rows_estimate"] == max(source["planner_rows"], 25000)
    assert source["planner_rows"] < 40000
    assert verify(conn, "sampled_narrow", "v", result.predicates).ok


def test_special_values_are_left_out_but_still_read(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v date) WITH (autovacuum_enabled = false)", t="sampled_infinite")
    run(
        conn,
        "INSERT INTO {t} SELECT CASE WHEN g % 10 = 0 THEN 'infinity'::date"
        " WHEN g % 10 = 1 THEN '-infinity'::date ELSE date '2000-01-01' + g END"
        " FROM generate_series(1, 30000) AS g",
        t="sampled_infinite",
    )
    result = plan_from_sample(conn, "sampled_infinite", "v", 4, sample_rows=WHOLE)
    source: Any = result.source
    assert source["unestimated_rows"] == 6000
    assert [w["code"] for w in source["warnings"]] == ["special_values_left_out"]
    assert result.total_rows == 24000
    assert verify(conn, "sampled_infinite", "v", result.predicates).ok


def test_a_partitioned_table_is_sampled_with_its_partitions(
    conn: psycopg.Connection[Any],
) -> None:
    run(conn, "CREATE TABLE {p} (v integer) PARTITION BY RANGE (v)", p="sampled_parted")
    for name, bounds in (
        ("sampled_parted_a", "(MINVALUE) TO (5000)"),
        ("sampled_parted_b", "(5000) TO (MAXVALUE)"),
    ):
        run(
            conn,
            "CREATE TABLE {c} PARTITION OF {p} FOR VALUES FROM "
            + bounds
            + " WITH (autovacuum_enabled = false)",
            p="sampled_parted",
            c=name,
        )
    run(conn, "INSERT INTO {p} SELECT g FROM generate_series(1, 10000) AS g", p="sampled_parted")
    check_whole(
        conn, "sampled_parted", plan_from_sample(conn, "sampled_parted", "v", 4, sample_rows=WHOLE)
    )
    sampled = plan_from_sample(conn, "sampled_parted", "v", 4, sample_rows=2000, seed=3)
    assert sampled.source["method"] in {"bernoulli", "system"}  # type: ignore[index]
    assert verify(conn, "sampled_parted", "v", sampled.predicates).ok


def test_the_callers_transaction_and_settings_are_left_as_they_were(
    conn: psycopg.Connection[Any],
) -> None:
    run(conn, "CREATE TABLE {t} (v timestamptz) WITH (autovacuum_enabled = false)", t="sampled_tz")
    run(
        conn,
        "INSERT INTO {t} SELECT timestamptz '2026-01-01 00:00:00+00' + g * interval '1 hour'"
        " FROM generate_series(1, 1000) AS g",
        t="sampled_tz",
    )
    with conn.transaction():
        conn.execute("SET LOCAL TimeZone = 'Asia/Kolkata'")
        conn.execute("SET LOCAL DateStyle = 'SQL, DMY'")
        result = plan_from_sample(conn, "sampled_tz", "v", 4, statement_timeout_ms=60000)
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        settings = conn.execute(
            "SELECT current_setting('TimeZone'), current_setting('DateStyle'),"
            " current_setting('transaction_read_only'), current_setting('statement_timeout')"
        ).fetchone()
        assert settings == ("Asia/Kolkata", "SQL, DMY", "off", "0")
    assert result.parts[1].sql.endswith("+00'::timestamptz")
    assert verify(conn, "sampled_tz", "v", result.predicates).ok


def test_the_strategy_reads_a_sample_without_statistics(conn: psycopg.Connection[Any]) -> None:
    run(conn, "CREATE TABLE {t} (v integer) WITH (autovacuum_enabled = false)", t="sampled_api")
    run(conn, "INSERT INTO {t} SELECT g FROM generate_series(1, 500) AS g", t="sampled_api")
    result = plan(conn, "sampled_api", "v", 4, strategy="sample")
    assert result.total_rows == 500
    assert result.report.splitlines()[1].startswith("from a sample of 500 rows, planned in ")


@pytest.fixture(scope="module")
def ten_million(database: Database) -> Iterator[str]:
    """Ten million rows in an order unrelated to their values, dropped afterwards."""
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        name = sql.Identifier(database.schema, "sampled_ten_million")
        admin.execute(
            sql.SQL(
                "CREATE UNLOGGED TABLE {} WITH (autovacuum_enabled = false) AS"
                " SELECT CASE WHEN g % 10 = 0 THEN 42 ELSE g::bigint * 7919 % 100000007 END AS v"
                " FROM generate_series(1, 10000000) AS g"
            ).format(name)
        )
        try:
            yield f"{database.schema}.sampled_ten_million"
        finally:
            admin.execute(sql.SQL("DROP TABLE {}").format(name))


def test_planning_a_ten_million_row_table_reads_a_small_sample(
    database: Database, ten_million: str, record_testsuite_property: Callable[[str, object], None]
) -> None:
    result = plan(database.dsn, ten_million, "v", 16, strategy="sample", seed=1)
    source: Any = result.source
    assert source["method"] == "system"
    assert 10000 <= source["sample_rows"] <= 90000
    assert 5000000 <= source["rows_estimate"] <= 15000000
    assert '"v" = 42' in result.predicates
    record_testsuite_property("ten_million_rows_sample_planning_ms", source["planning_ms"])
    record_testsuite_property("ten_million_rows_sample_rows", source["sample_rows"])
    # Generous, for a shared runner; it takes well under a second.
    assert source["planning_ms"] < 30000


def test_the_timeout_cancels_a_slow_sample(database: Database, ten_million: str) -> None:
    with pytest.raises(psycopg.errors.QueryCanceled):
        plan(
            database.dsn,
            ten_million,
            "v",
            16,
            strategy="sample",
            sample_rows=5000000,
            statement_timeout_ms=1,
        )
