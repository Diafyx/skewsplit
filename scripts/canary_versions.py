"""Find the upcoming releases that canary.yml tests skewsplit against.

Usage: python scripts/canary_versions.py --uv-pythons FILE

Each is newer than anything CI tests, or empty when there is none:

- ``python``: the newest CPython that uv can install, of a minor version newer
  than every supported Python (FILE is ``uv python list --only-downloads
  --output-format json``);
- ``pyspark``: the newest PySpark release, pre-releases included, of a line
  newer than every pinned line, with ``pyspark-line`` its line;
- ``postgres-image``: the newest PostgreSQL beta or release candidate of a
  major version newer than ci.yml's, as an image pinned by digest, with
  ``postgres-major`` its major version;
- ``pgjdbc``: the PostgreSQL JDBC driver's newest release, when it is newer
  than the Makefile's.

``spark-line`` and ``spark-python``, the newest pinned Spark line and its newest
Python, are always set: the PySpark and JDBC driver jobs run on them.

Writes each to $GITHUB_OUTPUT when that is set, and prints it. For each one
that is empty, prints a notice that the job testing it is skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spark_matrix import LINES, PYTHONS

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
PYSPARK_URL = "https://pypi.org/pypi/pyspark/json"
POSTGRES_URL = "https://hub.docker.com/v2/repositories/library/postgres/tags?page_size=100&name={}"
PGJDBC_URL = "https://repo1.maven.org/maven2/org/postgresql/postgresql/maven-metadata.xml"
TIMEOUT = 30
USER_AGENT = "skewsplit-canary"
# Pre-releases sort before the final release of the same version.
_STAGES = {"dev": 0, "a": 0, "preview": 1, "b": 1, "beta": 1, "rc": 2, "": 3}
_PYTHON = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?")
_PYSPARK = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\.?(dev|preview|rc)(\d+))?")
_POSTGRES = re.compile(r"(\d+)(beta|rc)(\d+)")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_RELEASE = re.compile(r"<release>([0-9]+(?:\.[0-9]+)*)</release>")


def _key(numbers: tuple[str, ...], stage: str | None, number: str | None) -> tuple[int, ...]:
    return (*map(int, numbers), _STAGES[stage or ""], int(number or 0))


def _minor(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def next_python(downloads: list[dict[str, Any]]) -> str:
    """Return the newest CPython download of a minor version above PYTHONS."""
    newest = _minor(PYTHONS[-1])
    found = {}
    for entry in downloads:
        if entry.get("implementation") != "cpython" or entry.get("variant") != "default":
            continue
        match = _PYTHON.fullmatch(str(entry.get("version")))
        if match and (int(match[1]), int(match[2])) > newest:
            found[_key(match.groups()[:3], match[4], match[5])] = match[0]
    return found[max(found)] if found else ""


def next_pyspark(releases: dict[str, list[dict[str, Any]]]) -> str:
    """Return the newest PySpark release of a line above every pinned one."""
    newest = max(_minor(line) for line in LINES)
    found = {}
    for version, files in releases.items():
        match = _PYSPARK.fullmatch(version)
        live = any(not file.get("yanked") for file in files)
        if match and live and (int(match[1]), int(match[2])) > newest:
            found[_key(match.groups()[:3], match[4], match[5])] = match[0]
    return found[max(found)] if found else ""


def newest_ci_postgres(ci: str) -> int:
    """Return the newest PostgreSQL major version that ci.yml tests."""
    majors = [int(major) for major in re.findall(r'^\s+- postgres: "(\d+)"$', ci, re.MULTILINE)]
    if not majors:
        msg = "ci.yml lists no PostgreSQL versions"
        raise ValueError(msg)
    return max(majors)


def next_postgres(tags: list[dict[str, Any]], newest: int) -> tuple[str, str]:
    """Return the newest beta or release candidate image above newest, and its major."""
    found = {}
    for tag in tags:
        match = _POSTGRES.fullmatch(str(tag.get("name")))
        digest = str(tag.get("digest"))
        if match and _DIGEST.fullmatch(digest) and int(match[1]) > newest:
            key = (int(match[1]), _STAGES[match[2]], int(match[3]))
            found[key] = (f"postgres:{match[0]}@{digest}", match[1])
    return found[max(found)] if found else ("", "")


def pinned_pgjdbc(makefile: str) -> str:
    """Return the JDBC driver version that the Makefile pins."""
    match = re.search(r"^PGJDBC_VERSION := (\S+)$", makefile, re.MULTILINE)
    if match is None:
        msg = "the Makefile pins no PGJDBC_VERSION"
        raise ValueError(msg)
    return match[1]


def next_pgjdbc(metadata: str, pinned: str) -> str:
    """Return Maven Central's newest release of the driver if it is newer than pinned."""
    match = _RELEASE.search(metadata)
    if match is None:
        msg = "the Maven metadata names no release"
        raise ValueError(msg)
    newer = tuple(map(int, match[1].split("."))) > tuple(map(int, pinned.split(".")))
    return match[1] if newer else ""


def fetch(url: str) -> str:
    """Return the body of an https URL."""
    if not url.startswith("https://"):
        msg = f"not an https URL: {url}"
        raise ValueError(msg)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 -- https only, checked above
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 -- https only, checked above
        return str(response.read().decode("utf-8"))


def resolve(uv_pythons: list[dict[str, Any]], get: Callable[[str], str]) -> dict[str, str]:
    """Return every output, empty for each kind with nothing newer to test."""
    pyspark = next_pyspark(json.loads(get(PYSPARK_URL))["releases"])
    tags = [
        tag
        for stage in ("beta", "rc")
        for tag in json.loads(get(POSTGRES_URL.format(stage)))["results"]
    ]
    newest = newest_ci_postgres(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    image, major = next_postgres(tags, newest)
    pinned = pinned_pgjdbc((ROOT / "Makefile").read_text(encoding="utf-8"))
    newest_line = max(LINES, key=_minor)
    return {
        "python": next_python(uv_pythons),
        "pyspark": pyspark,
        "pyspark-line": ".".join(pyspark.split(".")[:2]) if pyspark else "",
        "postgres-image": image,
        "postgres-major": major,
        "pgjdbc": next_pgjdbc(get(PGJDBC_URL), pinned),
        "spark-line": newest_line,
        "spark-python": LINES[newest_line][1],
    }


NOTICES = {
    "python": f"No Python newer than {PYTHONS[-1]} to install; python-next is skipped.",
    "pyspark": f"No PySpark line newer than {max(LINES, key=_minor)}; pyspark-next is skipped.",
    "postgres-image": (
        "No PostgreSQL beta or release candidate newer than CI's; postgres-next is skipped."
    ),
    "pgjdbc": "No JDBC driver newer than the Makefile's; pgjdbc-next is skipped.",
}


def main(argv: list[str] | None = None) -> int:
    """Print the outputs and notices, and write the outputs to $GITHUB_OUTPUT."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uv-pythons", type=Path, required=True)
    args = parser.parse_args(argv)
    found = resolve(json.loads(args.uv_pythons.read_text(encoding="utf-8")), get=fetch)
    lines = "".join(f"{name}={value}\n" for name, value in found.items())
    print(lines, end="")
    for name, notice in NOTICES.items():
        if not found[name]:
            print(f"::notice title=Nothing to test::{notice}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as file:
            file.write(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
