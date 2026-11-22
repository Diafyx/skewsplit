"""Open a connection, or use the caller's; bound its queries; and run caller SQL safely."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

# For the transaction in progress only, so the caller's setting comes back.
_TIMEOUT: Final = "SELECT pg_catalog.set_config('statement_timeout', %(timeout)s, true)"


@contextmanager
def connection(source: str | psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    """Yield a connection for ``source``.

    A string is a libpq connection string or URI; an empty one uses the
    ``PG*`` environment variables. The connection is opened in autocommit
    mode and closed afterwards. An existing connection is used as it is and
    left open.
    """
    if not isinstance(source, str):
        yield source
        return
    with psycopg.connect(source, autocommit=True) as conn:
        yield conn


def check_timeout(statement_timeout_ms: int | None) -> None:
    """Check a ``statement_timeout`` in milliseconds: ``None``, or positive.

    Raises:
        ValueError: If it is 0 or negative.
    """
    if statement_timeout_ms is not None and statement_timeout_ms <= 0:
        msg = f"statement_timeout_ms must be positive, not {statement_timeout_ms}"
        raise ValueError(msg)


def set_timeout(cursor: psycopg.Cursor[Any], statement_timeout_ms: int | None) -> None:
    """Set ``statement_timeout`` for the rest of the transaction, unless it is ``None``."""
    if statement_timeout_ms is not None:
        cursor.execute(_TIMEOUT, {"timeout": str(statement_timeout_ms)})


def caller_sql(text: str) -> sql.SQL:
    """A condition the caller wrote, such as a predicate or a filter, for :func:`run`.

    It goes in parentheses on lines of its own, so a trailing ``--`` comment
    ends with it, and its ``%`` signs are doubled, because :func:`run` always
    passes parameters. It is SQL from the caller and is not checked: only run
    text the caller trusts.
    """
    return sql.SQL("(\n" + text.replace("%", "%%") + "\n)")


def where_clause(where: str | None) -> sql.SQL | sql.Composed:
    """`` WHERE`` and the caller's filter, or nothing without one."""
    return sql.SQL("") if where is None else sql.Composed([sql.SQL(" WHERE "), caller_sql(where)])


def run(
    cursor: psycopg.Cursor[Any],
    query: sql.SQL | sql.Composed,
    params: Mapping[str, object] | None = None,
) -> psycopg.Cursor[Any]:
    """Execute a query that contains caller SQL, as one statement only.

    Without parameters, psycopg would send a query with the simple protocol,
    which runs every statement separated by ``;``: a predicate or filter
    with ``; COMMIT; ...`` could then end the read-only transaction and run
    more. Binary results make psycopg use the extended protocol, where the
    server refuses more than one statement.
    """
    return cursor.execute(query, params or {}, binary=True)
