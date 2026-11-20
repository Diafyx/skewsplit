from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from skewsplit.postgres.catalog import CatalogError, Column, TableFacts, find_column, table_facts
from skewsplit.postgres.connection import connection

if TYPE_CHECKING:
    from integration.conftest import Database


def code_of(conn: psycopg.Connection[Any], table: str, column: str) -> str:
    with pytest.raises(CatalogError) as caught:
        find_column(conn, table, column)
    return caught.value.code


def relid(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute("SELECT %s::regclass::oid", (table,)).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize(
    ("column", "type_name", "scale"),
    [
        ("c_int2", "int2", None),
        ("c_int4", "int4", None),
        ("c_int8", "int8", None),
        ("c_serial", "int4", None),
        ("c_bigserial", "int8", None),
        ("c_numeric", "numeric", 2),
        ("c_numeric_negative_scale", "numeric", 0),
        ("c_numeric_widest", "numeric", 1000),
        ("c_date", "date", None),
        ("c_timestamp", "timestamp", None),
        ("c_timestamp_ms", "timestamp", None),
        ("c_timestamptz", "timestamptz", None),
        ("Mixed Case", "int4", None),
    ],
)
def test_every_supported_type(
    conn: psycopg.Connection[Any],
    database: Database,
    column: str,
    type_name: str,
    scale: int | None,
) -> None:
    table = f"{database.schema}.types"
    assert find_column(conn, table, column) == Column(
        schema=database.schema,
        table="types",
        name=column,
        qualified=table,
        type_name=type_name,
        scale=scale,
        relid=relid(conn, table),
    )


@pytest.mark.parametrize(
    ("column", "sql_type"),
    [
        ("c_numeric_unconstrained", "numeric"),
        ("c_text", "text"),
        ("c_uuid", "uuid"),
        ("c_float", "double precision"),
        ("c_boolean", "boolean"),
        ("c_time", "time without time zone"),
        ("c_array", "integer[]"),
        ("c_domain", "{schema}.positive"),
    ],
)
def test_unsupported_types(
    conn: psycopg.Connection[Any], database: Database, column: str, sql_type: str
) -> None:
    with pytest.raises(CatalogError) as caught:
        find_column(conn, f"{database.schema}.types", column)
    assert caught.value.code == "unsupported_type"
    expected = sql_type.format(schema=database.schema)
    assert f"is {expected}; skewsplit splits smallint," in caught.value.message


def test_names_follow_sql_rules(conn: psycopg.Connection[Any], database: Database) -> None:
    schema = database.schema
    # Unquoted names fold to lower case; quoted ones keep their case and spaces.
    assert find_column(conn, f"{schema.upper()}.TYPES", "c_int4").table == "types"
    column = find_column(conn, f'"{schema}"."Order Lines"', "id")
    assert (column.table, column.qualified) == ("Order Lines", f'{schema}."Order Lines"')
    # The current database may be named too.
    assert find_column(conn, f"{conn.info.dbname}.{schema}.types", "c_int4").table == "types"


def test_an_unqualified_name_uses_the_search_path(
    conn: psycopg.Connection[Any], database: Database
) -> None:
    assert code_of(conn, "types", "c_int4") == "table_not_found"
    conn.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.schema)))
    assert find_column(conn, "types", "c_int4").qualified == f"{database.schema}.types"


@pytest.mark.parametrize(
    "table",
    [
        "{schema}.nope",
        "nope",
        "no_such_schema.types",
        "",
        '"unterminated',
        "a.b.c.d",
        "other_database.{schema}.types",
        "{schema}.types\0",
    ],
)
def test_tables_that_do_not_exist(
    conn: psycopg.Connection[Any], database: Database, table: str
) -> None:
    assert code_of(conn, table.format(schema=database.schema), "c_int4") == "table_not_found"


@pytest.mark.parametrize(
    ("name", "what"),
    [
        ("a_view", "a view"),
        ("a_sequence", "a sequence"),
        ("an_index", "an index"),
        ("a_composite", "a composite type"),
    ],
)
def test_relations_without_statistics_are_not_tables(
    conn: psycopg.Connection[Any], database: Database, name: str, what: str
) -> None:
    with pytest.raises(CatalogError) as caught:
        find_column(conn, f"{database.schema}.{name}", "id")
    assert caught.value.code == "not_a_table"
    assert caught.value.message.startswith(f"{database.schema}.{name} is {what};")


@pytest.mark.parametrize("name", ["a_matview", "a_partitioned"])
def test_materialized_views_and_partitioned_tables_have_statistics(
    conn: psycopg.Connection[Any], database: Database, name: str
) -> None:
    assert find_column(conn, f"{database.schema}.{name}", "id").type_name == "int4"


@pytest.mark.parametrize("column", ["nope", "C_INT4", "mixed case", "xmin", "ctid", ""])
def test_columns_that_do_not_exist(
    conn: psycopg.Connection[Any], database: Database, column: str
) -> None:
    assert code_of(conn, f"{database.schema}.types", column) == "column_not_found"


def stats_columns(conn: psycopg.Connection[Any], schema: str, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT attname FROM pg_stats WHERE schemaname = %s AND tablename = %s", (schema, table)
    ).fetchall()
    return {row[0] for row in rows}


def test_pg_stats_hides_exactly_what_find_column_refuses(
    conn: psycopg.Connection[Any], reader: psycopg.Connection[Any], database: Database
) -> None:
    schema = database.schema
    assert stats_columns(conn, schema, "secret") == {"id", "amount"}
    assert stats_columns(conn, schema, "guarded") == {"id"}
    assert stats_columns(reader, schema, "secret") == {"id"}
    assert stats_columns(reader, schema, "guarded") == set()
    assert find_column(reader, f"{schema}.secret", "id").name == "id"
    assert code_of(reader, f"{schema}.secret", "amount") == "permission_denied"
    assert code_of(reader, f"{schema}.guarded", "id") == "permission_denied"
    # The owner (here a superuser) is not subject to row-level security.
    assert find_column(conn, f"{schema}.guarded", "id").name == "id"


def test_a_schema_the_role_may_not_use(reader: psycopg.Connection[Any], database: Database) -> None:
    assert code_of(reader, f"{database.hidden}.t", "id") == "permission_denied"
    # Without USAGE the search_path skips the schema, so the table is not found.
    reader.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(database.hidden)))
    assert code_of(reader, "t", "id") == "table_not_found"


def test_a_failed_lookup_leaves_the_callers_transaction_usable(database: Database) -> None:
    with psycopg.connect(database.dsn, row_factory=dict_row) as conn:
        conn.execute("SELECT 1")
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        assert code_of(conn, "a.b.c.d", "id") == "table_not_found"
        assert code_of(conn, "nope", "id") == "table_not_found"
        column = find_column(conn, f"{database.schema}.types", "c_int4")
        assert column.type_name == "int4"
        assert table_facts(conn, column).modified_since_analyze is not None
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        assert conn.execute("SELECT 2 AS two").fetchone() == {"two": 2}


def test_a_connection_from_a_dsn(database: Database) -> None:
    with connection(database.dsn) as conn:
        assert conn.autocommit
        assert find_column(conn, f"{database.schema}.types", "c_int4").type_name == "int4"
    assert conn.closed


def flush_statistics(conn: psycopg.Connection[Any]) -> None:
    # This session's row counts reach the statistics system when it next goes
    # idle; this makes that happen after this statement (PostgreSQL 15 and later).
    conn.execute("SELECT pg_stat_force_next_flush()")


def test_table_facts(conn: psycopg.Connection[Any], database: Database) -> None:
    table = sql.Identifier(database.schema, "facts")
    conn.execute(
        sql.SQL("CREATE TABLE {} (id integer) WITH (autovacuum_enabled = false)").format(table)
    )
    conn.execute(sql.SQL("INSERT INTO {} SELECT generate_series(1, 100)").format(table))
    flush_statistics(conn)
    column = find_column(conn, f"{database.schema}.facts", "id")
    assert table_facts(conn, column) == TableFacts(None, None, 100)

    before = dt.datetime.now(dt.timezone.utc)
    conn.execute(sql.SQL("ANALYZE {}").format(table))
    facts = table_facts(conn, column)
    assert (facts.rows, facts.modified_since_analyze) == (100, 0)
    assert facts.analyzed_at is not None
    assert before - dt.timedelta(minutes=5) <= facts.analyzed_at <= before + dt.timedelta(minutes=5)

    # 7 rows inserted and 1 deleted.
    conn.execute(sql.SQL("INSERT INTO {} SELECT generate_series(101, 107)").format(table))
    conn.execute(sql.SQL("DELETE FROM {} WHERE id = 1").format(table))
    flush_statistics(conn)
    assert table_facts(conn, column).modified_since_analyze == 8
