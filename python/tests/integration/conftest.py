"""A PostgreSQL database for the integration tests.

``SKEWSPLIT_PG_DSN`` names a server where the connecting role is a superuser,
as in the py-integration CI job. The tests create two schemas and a role with
random names, and drop exactly those when they finish.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Iterator

DSN_VARIABLE = "SKEWSPLIT_PG_DSN"

SETUP = """
CREATE SCHEMA {schema};
CREATE SCHEMA {hidden};
CREATE ROLE {role} NOLOGIN;
GRANT USAGE ON SCHEMA {schema} TO {role};
CREATE DOMAIN {schema}.positive AS integer CHECK (VALUE > 0);
CREATE TABLE {schema}.types (
  c_int2 smallint,
  c_int4 integer,
  c_int8 bigint,
  c_serial serial,
  c_bigserial bigserial,
  c_numeric numeric(12, 2),
  c_numeric_negative_scale numeric(5, -2),
  c_numeric_widest numeric(1000, 1000),
  c_numeric_unconstrained numeric,
  c_date date,
  c_timestamp timestamp,
  c_timestamp_ms timestamp(3),
  c_timestamptz timestamptz,
  c_text text,
  c_uuid uuid,
  c_float double precision,
  c_boolean boolean,
  c_time time,
  c_array integer[],
  c_domain {schema}.positive,
  "Mixed Case" integer
);
CREATE TABLE {schema}."Order Lines" (id integer);
CREATE VIEW {schema}.a_view AS SELECT 1 AS id;
CREATE SEQUENCE {schema}.a_sequence;
CREATE INDEX an_index ON {schema}.types (c_int4);
CREATE TYPE {schema}.a_composite AS (id integer);
CREATE MATERIALIZED VIEW {schema}.a_matview AS SELECT 1 AS id;
CREATE TABLE {schema}.a_partitioned (id integer) PARTITION BY RANGE (id);
CREATE TABLE {schema}.secret (id integer, amount integer) WITH (autovacuum_enabled = false);
GRANT SELECT (id) ON {schema}.secret TO {role};
CREATE TABLE {schema}.guarded (id integer) WITH (autovacuum_enabled = false);
ALTER TABLE {schema}.guarded ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON {schema}.guarded TO {role};
INSERT INTO {schema}.secret SELECT g, g FROM generate_series(1, 100) AS g;
INSERT INTO {schema}.guarded SELECT g FROM generate_series(1, 100) AS g;
ANALYZE {schema}.secret;
ANALYZE {schema}.guarded;
CREATE TABLE {hidden}.t (id integer);
"""

TEARDOWN = """
DROP SCHEMA {schema} CASCADE;
DROP SCHEMA {hidden} CASCADE;
DROP ROLE {role};
"""


@dataclass(frozen=True)
class Database:
    """The test objects: ``schema`` holds the tables, and ``role`` may not use ``hidden``."""

    dsn: str
    schema: str
    hidden: str
    role: str

    def run(self, conn: psycopg.Connection[Any], template: str) -> None:
        """Run each statement of ``template`` with the test names filled in."""
        names = {
            "schema": sql.Identifier(self.schema),
            "hidden": sql.Identifier(self.hidden),
            "role": sql.Identifier(self.role),
        }
        for statement in template.split(";\n"):
            if statement.strip():
                conn.execute(sql.SQL(statement).format(**names))


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    dsn = os.environ.get(DSN_VARIABLE)
    if not dsn:
        pytest.fail(f"the integration tests need PostgreSQL: set {DSN_VARIABLE}")
    name = f"skewsplit_test_{secrets.token_hex(4)}"
    db = Database(dsn=dsn, schema=name, hidden=f"{name}_hidden", role=f"{name}_reader")
    with psycopg.connect(dsn, autocommit=True) as admin:
        # One transaction: if any statement fails, nothing was created, so
        # the teardown never drops an object these tests did not create.
        with admin.transaction():
            db.run(admin, SETUP)
        try:
            yield db
        finally:
            db.run(admin, TEARDOWN)


@pytest.fixture
def conn(database: Database) -> Iterator[psycopg.Connection[Any]]:
    """A superuser connection in autocommit mode."""
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def reader(database: Database) -> Iterator[psycopg.Connection[Any]]:
    """A connection acting as the test role, which has only the privileges granted to it."""
    with psycopg.connect(database.dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(database.role)))
        yield connection
