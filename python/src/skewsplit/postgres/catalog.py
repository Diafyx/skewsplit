"""Find the table and column to split, and the facts about the table's statistics.

Every query passes names as parameters: PostgreSQL's own ``to_regclass``
reads the table name, so it follows the rules of SQL and the connection's
``search_path``, and nothing is ever formatted into SQL text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn

import psycopg
from psycopg.rows import tuple_row

if TYPE_CHECKING:
    import datetime as dt

CatalogErrorCode = Literal[
    "table_not_found",
    "not_a_table",
    "column_not_found",
    "unsupported_type",
    "permission_denied",
    "statistics_missing",
]

# The fixed OIDs of the built-in types the planner can split (pg_type.dat).
_TYPES: Final = {
    21: "int2",
    23: "int4",
    20: "int8",
    1700: "numeric",
    1082: "date",
    1114: "timestamp",
    1184: "timestamptz",
}
_SUPPORTED: Final = (
    "smallint, integer, bigint, numeric with a declared scale, date, timestamp"
    " and timestamp with time zone"
)
# pg_stats has rows for these kinds of relation only.
_TABLE_KINDS: Final = frozenset("rpmf")
_KIND_NAMES: Final = {
    "v": "a view",
    "S": "a sequence",
    "i": "an index",
    "I": "a partitioned index",
    "c": "a composite type",
    "t": "a TOAST table",
}
# A numeric type modifier is ((precision << 16) | (scale & 0x7ff)) + VARHDRSZ,
# with the scale as an 11-bit two's complement number (numeric.c).
_VARHDRSZ: Final = 4
_SCALE_BITS: Final = 0x7FF
_SCALE_SIGN: Final = 0x400
# Errors to_regclass raises for a name it cannot read: invalid name syntax
# (PostgreSQL 15; later versions return NULL), too many dotted names, and a
# reference to another database.
_BAD_NAME: Final = frozenset({"42602", "42601", "0A000"})
_INSUFFICIENT_PRIVILEGE: Final = "42501"

_FIND_COLUMN: Final = """
SELECT c.oid, c.relkind::text, n.nspname, c.relname,
       quote_ident(n.nspname) || '.' || quote_ident(c.relname),
       c.relrowsecurity AND row_security_active(c.oid),
       a.atttypid, a.atttypmod, format_type(a.atttypid, a.atttypmod),
       has_column_privilege(c.oid, a.attnum, 'SELECT')
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_attribute a
  ON a.attrelid = c.oid AND a.attname = %(column)s AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.oid = pg_catalog.to_regclass(%(table)s)
"""

_TABLE_FACTS: Final = """
SELECT c.reltuples, GREATEST(s.last_analyze, s.last_autoanalyze), s.n_mod_since_analyze
FROM pg_catalog.pg_class c
LEFT JOIN pg_catalog.pg_stat_all_tables s ON s.relid = c.oid
WHERE c.oid = %(relid)s::oid
"""


class CatalogError(Exception):
    """The table or column cannot be split, or its statistics cannot be read.

    Attributes:
        code: What went wrong, for programs.
        message: A description for people.
    """

    def __init__(self, code: CatalogErrorCode, message: str) -> None:
        """Create an error with a code and a message."""
        super().__init__(message)
        self.code: CatalogErrorCode = code
        self.message = message


@dataclass(frozen=True)
class Column:
    """A column the planner can split, as the catalog describes it.

    Attributes:
        schema: The table's schema, as stored in the catalog.
        table: The table's name, as stored in the catalog.
        name: The column's name, as stored in the catalog.
        qualified: The schema-qualified table name, quoted where SQL needs it.
        type_name: The spec type of the column (spec section 2).
        scale: The scale for ``numeric``, otherwise ``None``.
        relid: The table's OID.
    """

    schema: str
    table: str
    name: str
    qualified: str
    type_name: str
    scale: int | None
    relid: int


@dataclass(frozen=True)
class TableFacts:
    """How current the table's statistics are.

    Attributes:
        rows: The estimated row count (``pg_class.reltuples``), or ``None``
            before the first ``VACUUM`` or ``ANALYZE``.
        analyzed_at: The last ``ANALYZE``, by hand or by autovacuum, or ``None``.
        modified_since_analyze: Rows inserted, updated or deleted since then,
            or ``None`` where PostgreSQL keeps no count (foreign tables).
    """

    rows: int | None
    analyzed_at: dt.datetime | None
    modified_since_analyze: int | None


def find_column(conn: psycopg.Connection[Any], table: str, column: str) -> Column:
    """Find a column and check that the planner can split it.

    Args:
        conn: An open connection. A transaction in progress is left as it
            was: the lookup runs in a savepoint.
        table: The table as SQL names it, as in Spark's ``dbtable``: unquoted
            names fold to lower case and an unqualified name is looked up in
            the ``search_path``. For example ``sales.orders`` or
            ``"Sales"."Orders"``.
        column: The column's exact name, not quoted, as in Spark's
            ``partitionColumn``.

    Raises:
        CatalogError: If the table or column does not exist, the column's
            type is not supported, or ``pg_stats`` hides the column from the
            current role.
    """
    row = _look_up(conn, table, column)
    relid, kind, schema, relname, qualified, rls_active, type_oid, typmod, sql_type, can_read = row
    if kind not in _TABLE_KINDS:
        _fail(
            "not_a_table",
            f"{qualified} is {_KIND_NAMES.get(kind, f'of kind {kind!r}')}; skewsplit reads"
            " tables, partitioned tables, materialized views and foreign tables",
        )
    if type_oid is None:
        _fail("column_not_found", f"{qualified} has no column {column!r}")
    spec_type = column_type(type_oid, typmod)
    if spec_type is None:
        _fail(
            "unsupported_type",
            f"column {column!r} of {qualified} is {sql_type}; skewsplit splits {_SUPPORTED}",
        )
    if not can_read:
        _fail(
            "permission_denied",
            f"the current role may not read column {column!r} of {qualified},"
            " so pg_stats hides its statistics",
        )
    if rls_active:
        _fail(
            "permission_denied",
            f"row-level security applies to the current role on {qualified},"
            " so pg_stats hides its statistics",
        )
    type_name, scale = spec_type
    return Column(
        schema=schema,
        table=relname,
        name=column,
        qualified=qualified,
        type_name=type_name,
        scale=scale,
        relid=relid,
    )


def table_facts(conn: psycopg.Connection[Any], column: Column) -> TableFacts:
    """Read how current the statistics of the column's table are.

    Raises:
        CatalogError: ``table_not_found`` if the table was dropped since
            :func:`find_column` found it.
    """
    row = _fetch(conn, _TABLE_FACTS, {"relid": column.relid})
    if row is None:
        _fail("table_not_found", f"{column.qualified} no longer exists")
    reltuples, analyzed_at, modified = row
    # PostgreSQL 14 and later store -1 until the first VACUUM or ANALYZE.
    rows = round(reltuples) if reltuples >= 0 else None
    return TableFacts(rows=rows, analyzed_at=analyzed_at, modified_since_analyze=modified)


def column_type(type_oid: int, typmod: int) -> tuple[str, int | None] | None:
    """Return the spec type and scale for a column type, or ``None`` if unsupported.

    ``numeric`` needs a declared scale. A negative scale, allowed since
    PostgreSQL 15, rounds to a power of ten, so the values are integers and
    their scale is 0.
    """
    type_name = _TYPES.get(type_oid)
    if type_name != "numeric":
        return None if type_name is None else (type_name, None)
    # An undeclared precision and scale is typmod -1.
    if typmod < _VARHDRSZ:
        return None
    bits = (typmod - _VARHDRSZ) & _SCALE_BITS
    scale = (bits ^ _SCALE_SIGN) - _SCALE_SIGN
    return type_name, max(scale, 0)


def _look_up(conn: psycopg.Connection[Any], table: str, column: str) -> tuple[Any, ...]:
    """Run the column query; fail if the table name is invalid or has no table."""
    if "\0" in table:
        _fail("table_not_found", f"no table {table!r}: names cannot contain U+0000")
    if "\0" in column:
        _fail("column_not_found", f"no column {column!r}: names cannot contain U+0000")
    try:
        row = _fetch(conn, _FIND_COLUMN, {"table": table, "column": column})
    except psycopg.Error as error:
        code: CatalogErrorCode
        if error.sqlstate == _INSUFFICIENT_PRIVILEGE:
            code, message = "permission_denied", f"the current role cannot look up {table!r}"
        elif error.sqlstate in _BAD_NAME:
            code, message = "table_not_found", f"{table!r} is not a valid table name"
        else:
            raise
        raise CatalogError(code, f"{message}: {error}") from error
    if row is None:
        _fail("table_not_found", f"no table {table!r} (check the name and the search_path)")
    return row


def _fetch(
    conn: psycopg.Connection[Any], query: str, params: dict[str, object]
) -> tuple[Any, ...] | None:
    # The caller's row factory could return dicts; these queries need tuples.
    # The transaction is a savepoint inside one already in progress, and an
    # error rolls it back, so a failed lookup leaves the caller's transaction
    # usable.
    with conn.transaction(), conn.cursor(row_factory=tuple_row) as cursor:
        return cursor.execute(query, params).fetchone()


def _fail(code: CatalogErrorCode, message: str) -> NoReturn:
    raise CatalogError(code, message)
