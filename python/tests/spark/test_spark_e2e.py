"""Spark reads PostgreSQL with the predicates skewsplit plans, through its snippets."""

from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import skewsplit
from skewsplit import postgres

if TYPE_CHECKING:
    from spark.conftest import Database

# PySpark is installed for these tests only, so each test imports it: the
# other test runs still collect this module.

# 20000 rows: 1000 NULL, 7 in about a fifth of them, the rest spread out.
# ANALYZE reads every row of a table this small, so spec section 13 bounds
# the estimates.
SKEWED = """
CREATE TABLE {t} (id bigint, v integer) WITH (autovacuum_enabled = false);
INSERT INTO {t}
SELECT g, CASE WHEN g % 20 = 0 THEN NULL WHEN g % 4 = 0 THEN 7 ELSE (g * 7919) % 10007 END
FROM generate_series(1, 20000) AS g;
ANALYZE {t}
"""
ROWS = 20000
PARTITIONS = 8

# A column whose name has a backslash before u0022, which Scala 2.12 would
# read as a quote if the backslash were not escaped, and a non-ASCII letter.
AWKWARD_COLUMN = "v\\u0022 ü"
AWKWARD = """
CREATE TABLE {t} ({c} integer) WITH (autovacuum_enabled = false);
INSERT INTO {t} SELECT CASE WHEN g <= 900 THEN g END FROM generate_series(1, 1000) AS g;
ANALYZE {t}
"""

# A name with every kind of character a string literal can get wrong. Spark
# never reads with it: only its literals are checked.
NASTY_COLUMN = (
    'say "hi", it\'s \\back\\slash\\ \\u0022 \\\\u0022 new\nline \r \t \b \f'
    " \x01 \x7f \x85 \u2028 ü 😀 \U000e0001 $x ${y}"
)


@pytest.fixture(scope="module")
def skewed(database: Database) -> str:
    return database.create("skewed", SKEWED)


@pytest.fixture(scope="module")
def awkward(database: Database) -> str:
    return database.create("awkward", AWKWARD, c=AWKWARD_COLUMN)


def run_pyspark_snippet(snippet: str, spark: Any, database: Database, table: str) -> Any:  # noqa: ANN401 -- PySpark has no types here
    names: dict[str, Any] = {
        "spark": spark,
        "url": database.url,
        "table": table,
        "properties": database.properties,
    }
    exec(snippet, names)  # noqa: S102 -- the snippet under test, from skewsplit
    return names


def test_the_installed_spark_line() -> None:
    import pyspark  # noqa: PLC0415

    line = os.environ["SKEWSPLIT_SPARK"]
    assert pyspark.__version__.startswith(f"{line}.")


def test_a_read_with_the_predicates_returns_every_row_once(
    spark: Any,  # noqa: ANN401 -- PySpark has no types here
    database: Database,
    skewed: str,
) -> None:
    from pyspark.sql.functions import spark_partition_id  # noqa: PLC0415

    plan = postgres.plan(database.dsn, skewed, "v", PARTITIONS)
    df = run_pyspark_snippet(plan.to_pyspark(), spark, database, skewed)["df"]
    # Spark makes one partition of each predicate, in order.
    assert df.rdd.getNumPartitions() == len(plan.predicates)
    counted = df.groupBy(spark_partition_id().alias("p")).count().collect()
    sizes = [0] * len(plan.predicates)
    for row in counted:
        sizes[row["p"]] = row["count"]
    assert sum(sizes) == df.count() == ROWS
    assert len(df.select("id").distinct().collect()) == ROWS
    # Each partition holds exactly its predicate's rows.
    found = postgres.verify(database.dsn, skewed, "v", plan.predicates, counts=True)
    assert found.ok
    assert tuple(sizes) == found.predicate_rows
    # The skew of the partitions is the reported one, within the bound of
    # spec section 13: 2B x N / total + 0.001, with B rows in a bucket.
    histogram: Any = postgres.distribution(database.dsn, skewed, "v")["histogram"]
    bucket = math.ceil(histogram["rows"] / (len(histogram["bounds"]) - 1))
    actual = found.skew_milli(PARTITIONS)
    assert actual is not None
    assert plan.skew_score is not None
    bound = 2 * bucket * PARTITIONS / ROWS + 0.001
    assert abs(actual / 1000 - plan.skew_score) <= bound + 1e-9


def test_the_pyspark_snippet_with_an_awkward_column(
    spark: Any,  # noqa: ANN401 -- PySpark has no types here
    database: Database,
    awkward: str,
) -> None:
    plan = postgres.plan(database.dsn, awkward, AWKWARD_COLUMN, 4)
    names = run_pyspark_snippet(plan.to_pyspark(), spark, database, awkward)
    assert names["predicates"] == plan.predicates
    assert names["df"].count() == 1000


def scala_text(text: str) -> str:
    # Only for the test's own values, which need no escapes.
    assert re.fullmatch(r"[\w.:/=@-]*", text), text
    return f'"{text}"'


def utf8_hex(values: list[str]) -> str:
    return ",".join(value.encode("utf-8").hex() for value in values)


def test_the_scala_snippet_in_spark_shell(
    database: Database, awkward: str, jdbc_jar: str, tmp_path: Path
) -> None:
    import pyspark  # noqa: PLC0415

    plan = postgres.plan(database.dsn, awkward, AWKWARD_COLUMN, 4)
    nasty = skewsplit.plan(
        {
            "column": NASTY_COLUMN,
            "type": "int4",
            "null_rows": 1,
            "mcv": [],
            "histogram": {"bounds": ["1", "10"], "rows": 9},
        },
        2,
    )
    settings = [
        f"val url = {scala_text(database.url)}",
        f"val table = {scala_text(awkward)}",
        "val properties = new java.util.Properties()",
        *(
            f"properties.setProperty({scala_text(key)}, {scala_text(value)})"
            for key, value in database.properties.items()
        ),
        # Each line of the shell is compiled on its own, so these two need two names.
        "def utf8Hex(value: String): String =",
        '  value.getBytes("UTF-8").map(b => "%02x".format(b & 0xff)).mkString',
        'def hex(values: Array[String]): String = values.map(utf8Hex).mkString(",")',
    ]
    script = "\n".join(
        [
            *settings,
            plan.to_scala(),
            # Split, so the marker is not in the source the shell may echo.
            'println("SKEW" + "SPLIT rows " + df.count() + " " + hex(predicates))',
            nasty.to_scala(),
            'println("SKEW" + "SPLIT literals " + hex(predicates))',
            "sys.exit(0)",
            "",
        ]
    )
    path = tmp_path / "snippet.scala"
    path.write_text(script, encoding="utf-8")
    home = Path(pyspark.__file__).parent
    shell = [str(home / "bin" / "spark-shell"), "--master", "local[1]", "--jars", jdbc_jar]
    shell += ["--conf", "spark.ui.enabled=false", "-i", str(path)]
    done = subprocess.run(  # noqa: S603 -- spark-shell from the installed PySpark
        shell,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        env={**os.environ, "SPARK_HOME": str(home)},
        check=False,
    )
    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    markers = dict(re.findall(r"^SKEWSPLIT (rows|literals) (.*)$", done.stdout, re.MULTILINE))
    assert markers.get("rows") == f"1000 {utf8_hex(plan.predicates)}", output
    assert markers.get("literals") == utf8_hex(nasty.predicates), output
