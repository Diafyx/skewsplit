"""The PostgreSQL adapter: read a column's statistics for the planner.

It needs the ``postgres`` extra (``pip install 'skewsplit[postgres]'``), which
installs psycopg. psycopg also needs libpq; install ``psycopg[binary]`` as well
if the system has none.
"""

from __future__ import annotations

try:
    import psycopg  # noqa: F401 -- only checks that the driver is installed
except ModuleNotFoundError as error:
    # A module missing inside an installed psycopg keeps psycopg's own error.
    if error.name != "psycopg":
        raise
    msg = "skewsplit.postgres needs psycopg: pip install 'skewsplit[postgres]'"
    raise ModuleNotFoundError(msg, name="psycopg") from error
