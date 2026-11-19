"""Read the benchmark table with Spark in each way and record the task metrics.

Usage: python bench/run.py --jdbc-jar JAR --partitions N --runs R OUTPUT_JSON

Needs PySpark (bench/requirements.txt), Java, the PostgreSQL JDBC driver's
jar, and the libpq variables PGHOST, PGPORT, PGDATABASE and PGUSER for a
server where bench/skewed_table.sql ran. `make bench` does all of this; it
runs in CI only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
import urllib.request
from pathlib import Path
from typing import Any

from pyspark.sql import DataFrame, SparkSession

import bench_methods as m

# Rows per round trip. Without it the driver buffers each partition in memory.
FETCH_SIZE = "10000"
STATUS_TIMEOUT = 60.0


def connection() -> dict[str, str]:
    """JDBC options from the libpq environment variables."""
    env = os.environ
    url = f"jdbc:postgresql://{env['PGHOST']}:{env.get('PGPORT', '5432')}/{env['PGDATABASE']}"
    return {
        "url": url,
        "user": env["PGUSER"],
        "password": env.get("PGPASSWORD", ""),
        "driver": "org.postgresql.Driver",
        "fetchsize": FETCH_SIZE,
    }


def query(spark: SparkSession, jdbc: dict[str, str], sql: str) -> list[Any]:
    """Run a query on the server and return its first row."""
    return list(spark.read.format("jdbc").options(**jdbc, query=sql).load().collect()[0])


def frame(
    spark: SparkSession, jdbc: dict[str, str], method: str, column: str, partitions: int
) -> DataFrame:
    """The table as one method splits it."""
    reader = spark.read.format("jdbc").options(**jdbc, dbtable=m.quote(m.TABLE))
    if method == "default bounds":
        lo, hi = query(spark, jdbc, m.bounds_query(column))
        options = {"lowerBound": lo, "upperBound": hi, "numPartitions": str(partitions)}
        return reader.options(partitionColumn=column, **options).load()
    if method == "hash-mod":
        predicates = m.hash_mod(column, partitions)
    else:
        (bounds,) = query(spark, jdbc, m.quantile_query(column, partitions))
        predicates = m.quantile_ranges(column, m.COLUMNS[column], bounds)
    properties = {key: value for key, value in jdbc.items() if key != "url"}
    table = m.quote(m.TABLE)
    return spark.read.jdbc(jdbc["url"], table, predicates=predicates, properties=properties)


def fetch(url: str) -> Any:  # noqa: ANN401 -- JSON from Spark's status API
    """GET a page of Spark's status API on this machine."""
    if not url.startswith("http://127.0.0.1:"):
        msg = f"unexpected status URL {url}"
        raise ValueError(msg)
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 -- checked above
        return json.load(response)


def tasks(spark: SparkSession, group: str) -> list[dict[str, int]]:
    """Duration and rows read of each task in a job group, once Spark has recorded them."""
    context = spark.sparkContext
    base = f"{context.uiWebUrl}/api/v1/applications/{context.applicationId}"
    deadline = time.monotonic() + STATUS_TIMEOUT
    while True:
        jobs = [job for job in fetch(f"{base}/jobs") if job.get("jobGroup") == group]
        done = (
            j["status"] == "SUCCEEDED" and j["numCompletedTasks"] == j["numTasks"] for j in jobs
        )
        if jobs and all(done):
            break
        if time.monotonic() > deadline:
            msg = f"Spark did not report the tasks of {group} within {STATUS_TIMEOUT} s"
            raise RuntimeError(msg)
        time.sleep(0.5)
    found = []
    for stage in sorted({stage for job in jobs for stage in job["stageIds"]}):
        for attempt in fetch(f"{base}/stages/{stage}"):
            listing = f"{base}/stages/{stage}/{attempt['attemptId']}/taskList?length=100000"
            found += [
                {
                    "index": task["index"],
                    "ms": task["duration"],
                    "rows": task["taskMetrics"]["inputMetrics"]["recordsRead"],
                }
                for task in fetch(listing)
                if task["status"] == "SUCCESS"
            ]
    return sorted(found, key=lambda task: task["index"])


def measure(
    spark: SparkSession, jdbc: dict[str, str], method: str, column: str, partitions: int
) -> dict[str, Any]:
    """Plan and read the whole table once; the noop sink makes Spark fetch every row."""
    start = time.perf_counter()
    df = frame(spark, jdbc, method, column, partitions)
    planned = time.perf_counter()
    group = f"{column}/{method}/{time.monotonic_ns()}"
    spark.sparkContext.setJobGroup(group, group)
    df.write.format("noop").mode("overwrite").save()
    seconds = time.perf_counter() - planned
    return {"plan_seconds": planned - start, "seconds": seconds, "tasks": tasks(spark, group)}


def java_version() -> str:
    """The version in the release file of the JDK in JAVA_HOME."""
    release = Path(os.environ["JAVA_HOME"], "release")
    lines = release.read_text(encoding="utf-8").splitlines() if release.is_file() else []
    for line in lines:
        if line.startswith("JAVA_VERSION="):
            return line.split("=", 1)[1].strip('"')
    return "unknown"


def main(argv: list[str] | None = None) -> int:
    """Run every method on every column, interleaved, and write the results."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jdbc-jar", type=Path, required=True)
    parser.add_argument("--partitions", type=int, default=16)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    spark = (
        SparkSession.builder.master(f"local[{os.cpu_count()}]")
        .appName("skewsplit-bench")
        .config("spark.jars", str(args.jdbc_jar.resolve()))
        .config("spark.driver.memory", "4g")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.showConsoleProgress", "false")
        .getOrCreate()
    )
    jdbc = connection()
    (total,) = query(spark, jdbc, f"SELECT count(*) FROM {m.quote(m.TABLE)}")  # noqa: S608 -- fixed, quoted identifiers
    results = []
    for run in range(1, args.runs + 1):
        for column in m.COLUMNS:
            for method in m.METHODS:
                result = measure(spark, jdbc, method, column, args.partitions)
                read = sum(task["rows"] for task in result["tasks"])
                if read != total:
                    msg = f"{method} on {column} read {read} rows of {total}"
                    raise RuntimeError(msg)
                results.append({"column": column, "method": method, "run": run, **result})
                print(f"run {run}, {column}, {method}: {result['seconds']:.1f} s", flush=True)
    (pg_version,) = query(spark, jdbc, "SELECT current_setting('server_version')")
    environment = {
        "spark": spark.version,
        "java": java_version(),
        "python": platform.python_version(),
        "postgres": pg_version,
        "cpus": os.cpu_count(),
        "memory_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30),
        # GitHub's runner image, for example "ubuntu24 20261116.1".
        "runner": " ".join(os.environ.get(k, "") for k in ("ImageOS", "ImageVersion")).strip(),
    }
    report = {
        "table": m.TABLE,
        "rows": total,
        "partitions": args.partitions,
        "runs": args.runs,
        "columns": m.COLUMNS,
        "environment": environment,
        "results": results,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
