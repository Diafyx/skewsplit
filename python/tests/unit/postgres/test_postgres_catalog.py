"""The catalog lookups against a fake connection.

The queries themselves run against every supported PostgreSQL version in
tests/integration; these tests cover how their results are read.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import tuple_row

from skewsplit.postgres.catalog import (
    CatalogError,
    Column,
    TableFacts,
    column_type,
    find_column,
    table_facts,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from typing_extensions import Self


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn
        self.row: object = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.conn.closed_cursors += 1

    def execute(self, query: str, params: dict[str, object]) -> FakeCursor:
        self.conn.queries.append((query, params))
        result = self.conn.results.pop(0)
        if isinstance(result, Exception):
            raise result
        self.row = result
        return self

    def fetchone(self) -> object:
        return self.row


class FakeConnection:
    """Answers each query with the next result: a row, None or an error to raise."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.queries: list[tuple[str, dict[str, object]]] = []
        self.transactions = 0
        self.closed_cursors = 0

    def cursor(self, *, row_factory: object) -> FakeCursor:
        assert row_factory is tuple_row
        return FakeCursor(self)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transactions += 1
        yield


def connect(*results: object) -> tuple[FakeConnection, psycopg.Connection[Any]]:
    fake = FakeConnection(*results)
    return fake, cast("psycopg.Connection[Any]", fake)


def numeric_typmod(precision: int, scale: int) -> int:
    return ((precision << 16) | (scale & 0x7FF)) + 4


def catalog_row(  # noqa: PLR0913 -- one keyword per column of the query
    *,
    kind: str = "r",
    type_oid: int | None = 23,
    typmod: int = -1,
    sql_type: str = "integer",
    can_read: bool | None = True,
    rls_active: bool = False,
) -> tuple[object, ...]:
    return (
        16384,
        kind,
        "sales",
        "Orders",
        'sales."Orders"',
        rls_active,
        type_oid,
        typmod,
        sql_type,
        can_read,
    )


def error_of(*results: object, table: str = 'sales."Orders"', column: str = "id") -> CatalogError:
    _, conn = connect(*results)
    with pytest.raises(CatalogError) as caught:
        find_column(conn, table, column)
    return caught.value


@pytest.mark.parametrize(
    ("type_oid", "typmod", "expected"),
    [
        (21, -1, ("int2", None)),
        (23, -1, ("int4", None)),
        (20, -1, ("int8", None)),
        (1082, -1, ("date", None)),
        (1114, -1, ("timestamp", None)),
        (1114, 3, ("timestamp", None)),
        (1184, 6, ("timestamptz", None)),
        (1700, numeric_typmod(12, 2), ("numeric", 2)),
        (1700, numeric_typmod(1000, 1000), ("numeric", 1000)),
        (1700, numeric_typmod(1, 0), ("numeric", 0)),
        # A negative scale rounds to tens, hundreds and so on: the values are integers.
        (1700, numeric_typmod(5, -2), ("numeric", 0)),
        (1700, numeric_typmod(1000, -1000), ("numeric", 0)),
        (1700, -1, None),
        (25, -1, None),
        (701, -1, None),
        (1007, -1, None),
    ],
)
def test_column_type(type_oid: int, typmod: int, expected: tuple[str, int | None] | None) -> None:
    assert column_type(type_oid, typmod) == expected


def test_numeric_typmod_matches_postgres() -> None:
    # SELECT atttypmod for numeric(12, 2) and numeric(5, -2) in PostgreSQL.
    assert numeric_typmod(12, 2) == 786438
    assert numeric_typmod(5, -2) == 329730


def test_find_column() -> None:
    fake, conn = connect(catalog_row())
    assert find_column(conn, 'sales."Orders"', "id") == Column(
        schema="sales",
        table="Orders",
        name="id",
        qualified='sales."Orders"',
        type_name="int4",
        scale=None,
        relid=16384,
    )
    [(query, params)] = fake.queries
    assert "to_regclass(%(table)s)" in query
    assert "a.attname = %(column)s" in query
    assert params == {"table": 'sales."Orders"', "column": "id"}
    assert (fake.transactions, fake.closed_cursors) == (1, 1)


@pytest.mark.parametrize("kind", ["r", "p", "m", "f"])
def test_find_column_reads_every_kind_with_statistics(kind: str) -> None:
    row = catalog_row(kind=kind, type_oid=1700, typmod=numeric_typmod(10, 3))
    column = find_column(connect(row)[1], "t", "id")
    assert (column.type_name, column.scale) == ("numeric", 3)


@pytest.mark.parametrize(
    ("kind", "what"),
    [
        ("v", "a view"),
        ("S", "a sequence"),
        ("i", "an index"),
        ("I", "a partitioned index"),
        ("c", "a composite type"),
        ("t", "a TOAST table"),
        ("x", "of kind 'x'"),
    ],
)
def test_other_kinds_are_not_tables(kind: str, what: str) -> None:
    error = error_of(catalog_row(kind=kind))
    assert error.code == "not_a_table"
    assert error.message.startswith(f'sales."Orders" is {what}; skewsplit reads tables,')


def test_a_missing_table() -> None:
    error = error_of(None, table="nope")
    assert error.code == "table_not_found"
    assert error.message == "no table 'nope' (check the name and the search_path)"


@pytest.mark.parametrize(
    "error_class", [errors.InvalidName, errors.SyntaxError, errors.FeatureNotSupported]
)
def test_a_name_postgres_cannot_read(error_class: type[psycopg.Error]) -> None:
    error = error_of(error_class("bad name"), table="a.b.c.d")
    assert error.code == "table_not_found"
    assert error.message == "'a.b.c.d' is not a valid table name: bad name"
    assert isinstance(error.__cause__, error_class)


def test_a_schema_the_role_cannot_use() -> None:
    denied = errors.InsufficientPrivilege("permission denied for schema hidden")
    error = error_of(denied, table="hidden.t")
    assert error.code == "permission_denied"
    assert error.message == (
        "the current role cannot look up 'hidden.t': permission denied for schema hidden"
    )


def test_other_database_errors_pass_through() -> None:
    lost = psycopg.OperationalError("the connection is lost")
    _, conn = connect(lost)
    with pytest.raises(psycopg.OperationalError, match="the connection is lost"):
        find_column(conn, "t", "id")


@pytest.mark.parametrize(
    ("table", "column", "code"),
    [("t\0", "id", "table_not_found"), ("t", "i\0d", "column_not_found")],
)
def test_names_with_nul_are_never_sent(table: str, column: str, code: str) -> None:
    fake, conn = connect()
    with pytest.raises(CatalogError, match="names cannot contain U\\+0000") as caught:
        find_column(conn, table, column)
    assert caught.value.code == code
    assert fake.queries == []


def test_a_missing_column() -> None:
    error = error_of(catalog_row(type_oid=None, can_read=None), column="Nope")
    assert error.code == "column_not_found"
    assert error.message == "sales.\"Orders\" has no column 'Nope'"


@pytest.mark.parametrize(
    ("type_oid", "typmod", "sql_type"), [(25, -1, "text"), (1700, -1, "numeric")]
)
def test_an_unsupported_type(type_oid: int, typmod: int, sql_type: str) -> None:
    error = error_of(catalog_row(type_oid=type_oid, typmod=typmod, sql_type=sql_type))
    assert error.code == "unsupported_type"
    assert error.message == (
        f"column 'id' of sales.\"Orders\" is {sql_type}; skewsplit splits smallint, integer,"
        " bigint, numeric with a declared scale, date, timestamp and timestamp with time zone"
    )


def test_a_column_the_role_may_not_read() -> None:
    error = error_of(catalog_row(can_read=False))
    assert error.code == "permission_denied"
    assert error.message == (
        "the current role may not read column 'id' of sales.\"Orders\","
        " so pg_stats hides its statistics"
    )


def test_row_level_security_hides_the_statistics() -> None:
    error = error_of(catalog_row(rls_active=True))
    assert error.code == "permission_denied"
    assert error.message == (
        'row-level security applies to the current role on sales."Orders",'
        " so pg_stats hides its statistics"
    )


COLUMN = Column(
    schema="sales",
    table="Orders",
    name="id",
    qualified='sales."Orders"',
    type_name="int4",
    scale=None,
    relid=16384,
)
ANALYZED = dt.datetime(2026, 11, 19, 8, 30, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize(
    ("row", "facts"),
    [
        ((1234.6, ANALYZED, 17), TableFacts(1235, ANALYZED, 17)),
        ((0.0, ANALYZED, 0), TableFacts(0, ANALYZED, 0)),
        ((-1.0, None, 5), TableFacts(None, None, 5)),
        ((12.0, None, None), TableFacts(12, None, None)),
    ],
)
def test_table_facts(row: tuple[object, ...], facts: TableFacts) -> None:
    fake, conn = connect(row)
    assert table_facts(conn, COLUMN) == facts
    [(query, params)] = fake.queries
    assert "pg_stat_all_tables" in query
    assert params == {"relid": 16384}
    assert fake.transactions == 1


def test_table_facts_of_a_dropped_table() -> None:
    _, conn = connect(None)
    with pytest.raises(CatalogError, match=r'^sales\."Orders" no longer exists$') as caught:
        table_facts(conn, COLUMN)
    assert caught.value.code == "table_not_found"
