"""Spark and PostgreSQL for the end-to-end tests.

``SKEWSPLIT_PG_DSN`` names a PostgreSQL server where the connecting role is a
superuser, as for tests/integration. ``make spark-e2e`` sets the other two:
``SKEWSPLIT_PGJDBC_JAR``, the PostgreSQL JDBC driver, and ``SKEWSPLIT_SPARK``,
the Spark line that was installed, such as ``4.3``. The tests create a schema
with a random name and drop exactly that when they finish.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

if TYPE_CHECKING:
    from collections.abc import Iterator


def setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"the Spark tests need {name}; run them with make spark-e2e")
    return value


@dataclass(frozen=True)
class Database:
    """The test schema, and how Spark's JDBC reader connects to the server."""

    dsn: str
    schema: str
    url: str
    properties: dict[str, str]

    def create(self, name: str, setup: str, **columns: str) -> str:
        """Run ``setup`` with ``{t}`` as the table ``name``; return its name.

        Each keyword names a column for the placeholder of the same name.
        """
        names = {key: sql.Identifier(column) for key, column in columns.items()}
        table = sql.Identifier(self.schema, name)
        with psycopg.connect(self.dsn, autocommit=True) as admin:
            for statement in setup.split(";\n"):
                admin.execute(sql.SQL(statement).format(t=table, **names))
        return f"{self.schema}.{name}"


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    dsn = setting("SKEWSPLIT_PG_DSN")
    params: dict[str, Any] = conninfo_to_dict(dsn)
    url = (
        f"jdbc:postgresql://{params.get('host', 'localhost')}:{params.get('port', 5432)}"
        f"/{params.get('dbname', 'postgres')}"
    )
    properties = {"user": str(params.get("user", "postgres")), "driver": "org.postgresql.Driver"}
    if params.get("password"):
        properties["password"] = str(params["password"])
    schema = f"skewsplit_spark_{secrets.token_hex(4)}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield Database(dsn, schema, url, properties)
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(scope="session")
def jdbc_jar() -> str:
    return setting("SKEWSPLIT_PGJDBC_JAR")


@pytest.fixture(scope="session")
def spark(jdbc_jar: str) -> Iterator[Any]:
    from pyspark.sql import SparkSession  # noqa: PLC0415 -- installed for these tests only

    # The Python workers run the same Python as the tests.
    os.environ["PYSPARK_PYTHON"] = sys.executable
    session = (
        SparkSession.builder.master("local[2]")
        .appName("skewsplit-e2e")
        .config("spark.jars", jdbc_jar)
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .getOrCreate()
    )
    try:
        yield session
    finally:
        session.stop()
