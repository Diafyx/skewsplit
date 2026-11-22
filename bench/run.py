"""Read the benchmark table with Spark in each way and record the task metrics.

Usage: python bench/run.py --jdbc-jar JAR --plans DIR --event-log DIR --partitions N
           --runs R OUTPUT_JSON

Needs PySpark (bench/requirements.txt), Java, the PostgreSQL JDBC driver's
jar, the libpq variables PGHOST, PGPORT, PGDATABASE and PGUSER for a server
where bench/skewed_table.sql ran, and skewsplit's plans from bench/plan.py in
DIR. Spark writes its event log into the event log directory, which
bench/bench_report.py turns into the per-task chart. `make bench` does all of
this; it runs in CI only.
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
# The job group of the queries that plan a read, so no measured read counts them.
SETUP_GROUP = "setup"


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


def frame(  # noqa: PLR0913 -- one argument per input of the read
    spark: SparkSession,
    jdbc: dict[str, str],
    *,
    plans: Path,
    method: str,
    column: str,
    partitions: int,
) -> tuple[DataFrame, float | None]:
    """The table as one method splits it, and skewsplit's planning time for its methods."""
    reader = spark.read.format("jdbc").options(**jdbc, dbtable=m.quote(m.TABLE))
    if method == "default bounds":
        lo, hi = query(spark, jdbc, m.bounds_query(column))
        options = {"lowerBound": lo, "upperBound": hi, "numPartitions": str(partitions)}
        return reader.options(partitionColumn=column, **options).load(), None
    planned = None
    strategy = m.strategy_of(method)
    if strategy is None:
        predicates = m.hash_mod(column, partitions)
    else:
        predicates, planned = m.read_plan(m.plan_path(plans, column, strategy))
    properties = {key: value for key, value in jdbc.items() if key != "url"}
    table = m.quote(m.TABLE)
    df = spark.read.jdbc(jdbc["url"], table, predicates=predicates, properties=properties)
    return df, planned


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


def measure(  # noqa: PLR0913 -- one argument per input of the read
    spark: SparkSession,
    jdbc: dict[str, str],
    *,
    plans: Path,
    method: str,
    column: str,
    partitions: int,
) -> dict[str, Any]:
    """Plan and read the whole table once; the noop sink makes Spark fetch every row.

    skewsplit's plans were made before Spark started, so their planning time
    is the one skewsplit measured; the other methods' is the query here.
    """
    # A job group lasts until the next one is set. Without this, the bounds
    # query of the next read would run in this read's group, after its tasks
    # were counted, and the event log would have one task more.
    spark.sparkContext.setJobGroup(SETUP_GROUP, "planning queries, outside every measured read")
    start = time.perf_counter()
    df, planned = frame(
        spark, jdbc, plans=plans, method=method, column=column, partitions=partitions
    )
    ready = time.perf_counter()
    group = f"{column}/{method}/{time.monotonic_ns()}"
    spark.sparkContext.setJobGroup(group, group)
    df.write.format("noop").mode("overwrite").save()
    seconds = time.perf_counter() - ready
    return {
        "group": group,
        "plan_seconds": ready - start if planned is None else planned,
        "seconds": seconds,
        "tasks": tasks(spark, group),
    }


def run_url() -> str | None:
    """The URL of the GitHub Actions run, when there is one."""
    env = os.environ
    keys = ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    if not all(env.get(key) for key in keys):
        return None
    repository = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}"
    return f"{repository}/actions/runs/{env['GITHUB_RUN_ID']}"


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
    parser.add_argument("--plans", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
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
        # The event log is the record of every task that the chart is drawn from.
        .config("spark.eventLog.enabled", "true")
        .config("spark.eventLog.dir", args.event_log.resolve().as_uri())
        .config("spark.eventLog.compress", "false")
        .config("spark.eventLog.rolling.enabled", "false")
        .getOrCreate()
    )
    jdbc = connection()
    (total,) = query(spark, jdbc, f"SELECT count(*) FROM {m.quote(m.TABLE)}")  # noqa: S608 -- fixed, quoted identifiers
    results = []
    for run in range(1, args.runs + 1):
        for column in m.COLUMNS:
            for method in m.METHODS:
                result = measure(
                    spark,
                    jdbc,
                    plans=args.plans,
                    method=method,
                    column=column,
                    partitions=args.partitions,
                )
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
    application = spark.sparkContext.applicationId
    report = {
        "table": m.TABLE,
        "rows": total,
        "partitions": args.partitions,
        "runs": args.runs,
        "columns": m.COLUMNS,
        "environment": environment,
        "sample_seed": m.SAMPLE_SEED,
        "run_url": run_url(),
        # Spark names the event log after the application, and finishes it on stop().
        "event_log": application,
        "results": results,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
