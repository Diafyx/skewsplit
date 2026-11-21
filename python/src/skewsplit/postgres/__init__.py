"""The PostgreSQL adapter: plan from a table's statistics, and verify a plan.

It needs the ``postgres`` extra (``pip install 'skewsplit[postgres]'``), which
installs psycopg. psycopg also needs libpq; install ``psycopg[binary]`` as well
if the system has none.

:func:`plan` reads the column's statistics from ``pg_stats`` and never scans
the table, and :func:`distribution` returns what it read, for
:func:`skewsplit.plan`. :func:`verify` scans the table once, to check that
every row matches exactly one predicate. They all take a libpq connection
string or an open connection, which they leave as it was.

>>> import psycopg
>>> import skewsplit.postgres
>>> with psycopg.connect() as conn:  # connects with the PG* environment variables
...     result = skewsplit.postgres.plan(conn, "orders", "customer_id", 4)
...     found = skewsplit.postgres.verify(conn, "orders", "customer_id", result.predicates)
>>> result.predicates[0]
'"customer_id" IS NULL'
>>> found.ok
True

The predicates are the list Spark's JDBC reader takes::

    result = skewsplit.postgres.plan("dbname=shop", "orders", "customer_id", 16)
    orders = spark.read.jdbc(url, "orders", predicates=result.predicates, properties=properties)
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

from skewsplit.postgres.catalog import CatalogError, CatalogErrorCode
from skewsplit.postgres.statistics import distribution
from skewsplit.postgres.statistics import plan_from_statistics as plan
from skewsplit.postgres.verify import Verification, verify, verify_plan

__all__ = [
    "CatalogError",
    "CatalogErrorCode",
    "Verification",
    "distribution",
    "plan",
    "verify",
    "verify_plan",
]
