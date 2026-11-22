"""Choosing the strategy (spec section 17) on real tables: one test per row of its table."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql

from skewsplit.postgres.catalog import CatalogError
from skewsplit.postgres.planning import distribution, plan
from skewsplit.postgres.verify import verify

if TYPE_CHECKING:
    from collections.abc import Iterator

    from integration.conftest import Database
    from skewsplit.core.plan import Plan


def flush_statistics(conn: psycopg.Connection[Any]) -> None:
    # This session's row counts reach the statistics system when it next goes
    # idle; this makes that happen after this statement (PostgreSQL 15 and later).
    conn.execute("SELECT pg_stat_force_next_flush()")


def run(conn: psycopg.Connection[Any], template: str, **tables: str) -> None:
    """Run a statement with the test schema's tables filled in by name."""
    schema = conn.execute("SELECT current_schema()").fetchone()
    assert schema is not None
    names = {key: sql.Identifier(schema[0], name) for key, name in tables.items()}
    conn.execute(sql.SQL(template).format(**names))


@pytest.fixture
def conn(database: Database) -> Iterator[psycopg.Connection[Any]]:
    """A superuser connection whose search_path is the test schema."""
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
        yield connection


def filled(conn: psycopg.Connection[Any], table: str, sql_type: str = "integer") -> None:
    """Create ``table`` with 1000 rows, 1 to 1000, that autovacuum leaves alone."""
    run(
        conn,
        "CREATE TABLE {t} (v " + sql_type + ") WITH (autovacuum_enabled = false)",
        t=table,
    )
    run(conn, "INSERT INTO {t} SELECT g FROM generate_series(1, 1000) AS g", t=table)
    flush_statistics(conn)


def analyzed(conn: psycopg.Connection[Any], table: str) -> None:
    run(conn, "ANALYZE {t}", t=table)
    flush_statistics(conn)


def choice(result: Plan) -> tuple[str, str]:
    source: Any = result.source
    return source["auto"]["strategy"], source["auto"]["reason"]


def test_fresh_statistics_are_used(conn: psycopg.Connection[Any]) -> None:
    filled(conn, "auto_fresh")
    analyzed(conn, "auto_fresh")
    result = plan(conn, "auto_fresh", "v", 4, strategy="auto")
    assert choice(result) == ("statistics", "statistics_fresh")
    source: Any = result.source
    assert source["statistics"] == "pg_stats"
    assert result.report.splitlines()[1] == (
        "strategy statistics, chosen by auto: the statistics are fresh:"
        " 0 rows changed since the last ANALYZE, a tenth or less"
    )
    assert verify(conn, "auto_fresh", "v", result.predicates).ok


def test_a_table_never_analyzed_is_sampled(conn: psycopg.Connection[Any]) -> None:
    filled(conn, "auto_missing")
    result = plan(conn, "auto_missing", "v", 4, strategy="auto")
    assert choice(result) == ("sample", "statistics_missing")
    source: Any = result.source
    # 1000 rows are fewer than the sample size, so every row was read.
    assert (source["statistics"], source["method"], source["sample_rows"]) == (
        "sample",
        "all",
        1000,
    )
    assert result.report.splitlines()[1].startswith(
        "strategy sample, chosen by auto: no statistics to use: "
    )
    assert verify(conn, "auto_missing", "v", result.predicates).ok


def test_stale_statistics_are_sampled(conn: psycopg.Connection[Any]) -> None:
    filled(conn, "auto_stale")
    analyzed(conn, "auto_stale")
    # A tenth of the rows changed is not more than a tenth.
    run(conn, "UPDATE {t} SET v = -v WHERE v <= 100", t="auto_stale")
    flush_statistics(conn)
    assert choice(plan(conn, "auto_stale", "v", 4, strategy="auto")) == (
        "statistics",
        "statistics_fresh",
    )
    run(conn, "UPDATE {t} SET v = -v WHERE v = 101", t="auto_stale")
    flush_statistics(conn)
    result = plan(conn, "auto_stale", "v", 4, strategy="auto", seed=3)
    assert choice(result) == ("sample", "statistics_stale")
    source: Any = result.source
    assert source["auto"]["message"] == (
        "the statistics are out of date: 101 of about 1000 rows changed since the last ANALYZE"
    )
    # The sample sees the table as it is now, negative values included.
    assert source["sample_rows"] == 1000
    assert verify(conn, "auto_stale", "v", result.predicates).ok


def test_a_partitioned_table_is_sampled_since_its_changes_are_not_counted(
    conn: psycopg.Connection[Any],
) -> None:
    run(conn, "CREATE TABLE {p} (v integer) PARTITION BY RANGE (v)", p="auto_parted")
    for name, bounds in (
        ("auto_parted_a", "(MINVALUE) TO (500)"),
        ("auto_parted_b", "(500) TO (MAXVALUE)"),
    ):
        run(
            conn,
            "CREATE TABLE {c} PARTITION OF {p} FOR VALUES FROM "
            + bounds
            + " WITH (autovacuum_enabled = false)",
            p="auto_parted",
            c=name,
        )
    run(conn, "INSERT INTO {p} SELECT g FROM generate_series(1, 1000) AS g", p="auto_parted")
    flush_statistics(conn)
    analyzed(conn, "auto_parted")
    # The statistics exist, but PostgreSQL does not count changes to the tree.
    assert distribution(conn, "auto_parted", "v")["source"]["inherited"] is True  # type: ignore[index]
    result = plan(conn, "auto_parted", "v", 4, strategy="auto")
    assert choice(result) == ("sample", "statistics_unchecked")
    assert result.total_rows == 1000
    assert verify(conn, "auto_parted", "v", result.predicates).ok


def test_a_column_ranges_cannot_split_gets_hash_predicates(conn: psycopg.Connection[Any]) -> None:
    filled(conn, "auto_text", "text")
    analyzed(conn, "auto_text")
    result = plan(conn, "auto_text", "v", 4, strategy="auto")
    assert choice(result) == ("hash", "unsupported_type")
    source: Any = result.source
    assert source["statistics"] == "count"
    assert [part.kind for part in result.parts[1:]] == ["hash"] * 4
    assert result.report.splitlines()[1] == (
        "strategy hash, chosen by auto: ranges cannot split text, so hash predicates split it;"
        " each partition reads the whole table"
    )
    found = verify(conn, "auto_text", "v", result.predicates, counts=True)
    assert found.ok
    assert found.total_rows == 1000


def test_ranges_asked_for_on_a_text_column_are_an_error(conn: psycopg.Connection[Any]) -> None:
    filled(conn, "auto_ranges", "text")
    for call in (
        lambda: plan(conn, "auto_ranges", "v", 4, method="range", strategy="auto"),
        lambda: distribution(conn, "auto_ranges", "v", strategy="auto"),
    ):
        with pytest.raises(CatalogError) as raised:
            call()
        assert raised.value.code == "unsupported_type"


def test_the_distribution_says_which_strategy_was_chosen(conn: psycopg.Connection[Any]) -> None:
    filled(conn, "auto_distribution")
    data: Any = distribution(conn, "auto_distribution", "v", strategy="auto")
    assert data["source"]["auto"]["reason"] == "statistics_missing"
    assert data["source"]["statistics"] == "sample"
    analyzed(conn, "auto_distribution")
    data = distribution(conn, "auto_distribution", "v", strategy="auto")
    assert data["source"]["auto"]["reason"] == "statistics_fresh"
    assert data["source"]["statistics"] == "pg_stats"


def test_exact_is_never_chosen(conn: psycopg.Connection[Any]) -> None:
    filled(conn, "auto_never_exact")
    reasons = set()
    for step in ("missing", "analyzed"):
        if step == "analyzed":
            analyzed(conn, "auto_never_exact")
        source: Any = plan(conn, "auto_never_exact", "v", 4, strategy="auto").source
        reasons.add(source["auto"]["strategy"])
        assert source["statistics"] != "exact"
    assert reasons == {"sample", "statistics"}
