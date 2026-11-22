"""Open a connection from a DSN, or use one the caller already has, and bound its queries."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final

import psycopg

if TYPE_CHECKING:
    from collections.abc import Iterator

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
