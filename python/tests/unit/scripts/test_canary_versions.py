from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest

import canary_versions
from canary_versions import (
    PGJDBC_URL,
    POSTGRES_URL,
    PYSPARK_URL,
    ROOT,
    fetch,
    main,
    newest_ci_postgres,
    next_pgjdbc,
    next_postgres,
    next_pyspark,
    next_python,
    pinned_pgjdbc,
    resolve,
)

if TYPE_CHECKING:
    from pathlib import Path

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def python(
    version: str, implementation: str = "cpython", variant: str = "default"
) -> dict[str, str]:
    return {"version": version, "implementation": implementation, "variant": variant}


def test_next_python_is_the_newest_minor_above_the_supported_ones() -> None:
    downloads = [
        python("3.15.2"),
        python("3.16.0a1"),
        python("3.16.0b2"),
        python("3.16.0a3"),
        python("3.16.0rc1", variant="freethreaded"),
        python("3.17.0a1", implementation="pypy"),
    ]
    assert next_python(downloads) == "3.16.0b2"
    assert next_python([*downloads, python("3.16.0")]) == "3.16.0"
    assert next_python([*downloads, python("3.17.0a1")]) == "3.17.0a1"


def test_next_python_is_empty_without_a_newer_minor() -> None:
    assert next_python([python("3.15.2"), python("3.9.25"), {"version": None}]) == ""
    assert next_python([]) == ""


def files(*, yanked: bool = False) -> list[dict[str, object]]:
    return [{"filename": "pyspark.tar.gz", "yanked": yanked}]


def test_next_pyspark_is_the_newest_release_of_an_unpinned_line() -> None:
    releases = {
        "4.3.0": files(),
        "4.3.1rc1": files(),
        "4.4.0.dev1": files(),
        "4.4.0.dev2": files(),
        "4.4.0.preview1": files(),
        "4.4.0rc1": files(yanked=True),
        "4.10.0.dev1": files(),
        "nonsense": files(),
    }
    assert next_pyspark(releases) == "4.10.0.dev1"
    del releases["4.10.0.dev1"]
    assert next_pyspark(releases) == "4.4.0.preview1"
    assert next_pyspark({**releases, "4.4.0rc2": files()}) == "4.4.0rc2"
    assert next_pyspark({**releases, "4.4.0rc2": files(), "4.4.0": files()}) == "4.4.0"


def test_next_pyspark_is_empty_without_an_unpinned_line() -> None:
    assert next_pyspark({"3.5.9": files(), "4.3.1": files(), "4.4.0": []}) == ""


def test_newest_ci_postgres_reads_the_integration_matrix() -> None:
    assert newest_ci_postgres((ROOT / ".github" / "workflows" / "ci.yml").read_text()) == 19
    assert newest_ci_postgres('      - postgres: "9"\n        - postgres: "15"\n') == 15
    with pytest.raises(ValueError, match="no PostgreSQL versions"):
        newest_ci_postgres("postgres: 19\n")


def tag(name: str, digest: str | None = DIGEST) -> dict[str, object]:
    return {"name": name, "digest": digest}


def test_next_postgres_is_the_newest_beta_or_candidate_above_ci() -> None:
    tags = [
        tag("19rc2"),
        tag("20beta1"),
        tag("20beta2", OTHER),
        tag("20beta3-trixie"),
        tag("20beta3", None),
        tag("20beta3", "sha256:short"),
        tag("latest"),
    ]
    assert next_postgres(tags, 19) == (f"postgres:20beta2@{OTHER}", "20")
    assert next_postgres([*tags, tag("20rc1")], 19) == (f"postgres:20rc1@{DIGEST}", "20")
    assert next_postgres([*tags, tag("21beta1")], 19) == (f"postgres:21beta1@{DIGEST}", "21")


def test_next_postgres_images_are_pinned_by_digest() -> None:
    image, _ = next_postgres([tag("20beta1")], 19)
    assert re.fullmatch(r"postgres:20beta1@sha256:[0-9a-f]{64}", image)


def test_next_postgres_is_empty_without_a_newer_major() -> None:
    assert next_postgres([tag("19rc2"), tag("19beta4")], 19) == ("", "")


def test_pinned_pgjdbc_reads_the_makefile() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned_pgjdbc((ROOT / "Makefile").read_text()))
    assert pinned_pgjdbc("PGJDBC_VERSION := 42.7.14\n") == "42.7.14"
    with pytest.raises(ValueError, match="no PGJDBC_VERSION"):
        pinned_pgjdbc("PGJDBC_VERSION ?= 42\n")


@pytest.mark.parametrize(
    ("release", "found"),
    [("42.7.15", "42.7.15"), ("42.10.0", "42.10.0"), ("42.7.14", ""), ("42.7.9", "")],
)
def test_next_pgjdbc_is_a_newer_release(release: str, found: str) -> None:
    metadata = f"<metadata><versioning><release>{release}</release></versioning></metadata>"
    assert next_pgjdbc(metadata, "42.7.14") == found


def test_next_pgjdbc_needs_a_release() -> None:
    with pytest.raises(ValueError, match="names no release"):
        next_pgjdbc("<metadata><release>42.x</release></metadata>", "42.7.14")


def test_fetch_requests_https_only() -> None:
    with pytest.raises(ValueError, match="not an https URL"):
        fetch("http://pypi.org/pypi/pyspark/json")


def fake_get(
    pyspark: dict[str, Any],
    tags: list[dict[str, object]],
    release: str,
    candidates: list[dict[str, object]] | None = None,
) -> Any:  # noqa: ANN401 -- a stand-in for fetch
    answers = {
        PYSPARK_URL: json.dumps({"releases": pyspark}),
        POSTGRES_URL.format("beta"): json.dumps({"results": tags}),
        POSTGRES_URL.format("rc"): json.dumps({"results": candidates or []}),
        PGJDBC_URL: f"<release>{release}</release>",
    }
    return answers.__getitem__


def test_resolve_finds_every_kind() -> None:
    get = fake_get({"4.4.0.dev1": files()}, [tag("20beta1")], "99.0.0", [tag("20rc1", OTHER)])
    assert resolve([python("3.16.0a1")], get) == {
        "python": "3.16.0a1",
        "pyspark": "4.4.0.dev1",
        "pyspark-line": "4.4",
        "postgres-image": f"postgres:20rc1@{OTHER}",
        "postgres-major": "20",
        "pgjdbc": "99.0.0",
        "spark-line": "4.3",
        "spark-python": "3.14",
    }


def test_main_prints_notices_for_what_has_nothing_newer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    get = fake_get({"4.3.0": files()}, [tag("19rc2")], "1.0.0")
    monkeypatch.setattr(canary_versions, "fetch", get)
    pythons, output = tmp_path / "pythons.json", tmp_path / "output"
    pythons.write_text(json.dumps([python("3.16.0a1")]), encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main(["--uv-pythons", str(pythons)]) == 0
    printed = capsys.readouterr().out
    written = output.read_text(encoding="utf-8")
    assert written == (
        "python=3.16.0a1\npyspark=\npyspark-line=\npostgres-image=\npostgres-major=\n"
        "pgjdbc=\nspark-line=4.3\nspark-python=3.14\n"
    )
    assert printed.startswith(written)
    notices = re.findall(r"^::notice title=Nothing to test::(.*)$", printed, re.MULTILINE)
    assert [notice.split(";")[1].split()[0] for notice in notices] == [
        "pyspark-next",
        "postgres-next",
        "pgjdbc-next",
    ]


def test_the_canary_workflow_reads_every_output() -> None:
    canary = (ROOT / ".github" / "workflows" / "canary.yml").read_text(encoding="utf-8")
    pattern = r"^      ([a-z-]+): \$\{\{ steps\.versions\.outputs\.\1 \}\}$"
    get = fake_get({}, [], "1.0.0")
    assert set(re.findall(pattern, canary, re.MULTILINE)) == set(resolve([], get))
