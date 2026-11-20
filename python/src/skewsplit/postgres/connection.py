"""Open a connection from a DSN, or use one the caller already has."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import psycopg

if TYPE_CHECKING:
    from collections.abc import Iterator


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
