from __future__ import annotations

import doctest
import os
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

import skewsplit.postgres

if TYPE_CHECKING:
    import pytest

    from integration.conftest import Database

# The example's table: 100 NULL rows, 500 rows of 42, 60 of 7 and 340 others.
SETUP = """
CREATE TABLE {t} (customer_id integer) WITH (autovacuum_enabled = false);
INSERT INTO {t}
SELECT CASE WHEN g <= 100 THEN NULL WHEN g <= 600 THEN 42 WHEN g <= 660 THEN 7 ELSE g END
FROM generate_series(1, 1000) AS g;
ANALYZE {t}
"""

# The libpq environment variable of each connection parameter.
VARIABLES = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
}


def test_the_example_of_the_package(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    with psycopg.connect(database.dsn, autocommit=True) as admin:
        for statement in SETUP.split(";\n"):
            admin.execute(sql.SQL(statement).format(t=sql.Identifier(database.schema, "orders")))
    # The example connects with the PG* variables: the test server's, with
    # the test schema first on the search path.
    for name in list(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)
    for key, value in conninfo_to_dict(database.dsn).items():
        monkeypatch.setenv(VARIABLES[key], str(value))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={database.schema}")
    results = doctest.testmod(skewsplit.postgres)
    assert results == doctest.TestResults(failed=0, attempted=5)
