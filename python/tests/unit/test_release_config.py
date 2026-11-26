"""Checks that the release-please setup agrees with the packages and the PR rules.

A release pull request changes the manifest and the component's version files
together (python/pyproject.toml and uv.lock, or rust/Cargo.toml and
rust/Cargo.lock), so CI on that pull request runs these checks too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from pr_hygiene import PullRequest, check, is_exempt_bot

REPO = Path(__file__).parents[3]
CONFIG: dict[str, Any] = json.loads(
    (REPO / "release-please-config.json").read_text(encoding="utf-8")
)
MANIFEST: dict[str, str] = json.loads(
    (REPO / ".release-please-manifest.json").read_text(encoding="utf-8")
)
# release-please's defaults for the labels on an open release pull request and
# on a released one. The config does not override them.
PENDING_LABEL = "autorelease: pending"
TAGGED_LABEL = "autorelease: tagged"


def release_title(component: str, version: str) -> str:
    """Render the release pull request title as release-please does.

    release-please puts a space before a non-empty component.
    """
    pattern = str(CONFIG["pull-request-title-pattern"])
    return pattern.replace("${component}", f" {component}").replace("${version}", version)


def extra_labels() -> set[str]:
    return set(str(CONFIG["extra-label"]).split(","))


def test_every_package_has_a_manifest_version() -> None:
    assert set(CONFIG["packages"]) == set(MANIFEST)


def test_the_manifest_version_is_the_package_version() -> None:
    pyproject = (REPO / "python" / "pyproject.toml").read_text(encoding="utf-8")
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    project = re.findall(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    locked = re.findall(r'^name = "skewsplit"\nversion = "([^"]+)"$', lock, re.MULTILINE)
    assert project == locked == [MANIFEST["python"]]


def test_the_crate_version_is_the_manifest_version() -> None:
    manifest = (REPO / "rust" / "Cargo.toml").read_text(encoding="utf-8")
    lock = (REPO / "rust" / "Cargo.lock").read_text(encoding="utf-8")
    package = re.findall(r'^version = "([^"]+)"$', manifest, re.MULTILINE)
    locked = re.findall(r'^name = "skewsplit"\nversion = "([^"]+)"$', lock, re.MULTILINE)
    assert package == locked == [MANIFEST["rust"]]


@pytest.mark.parametrize(("path", "release_type"), [("python", "python"), ("rust", "rust")])
def test_each_component_is_named_after_its_folder(path: str, release_type: str) -> None:
    package = CONFIG["packages"][path]
    assert package["component"] == path
    assert package["release-type"] == release_type
    assert package["package-name"] == "skewsplit"
    # Relative to the folder: python/CHANGELOG.md and rust/CHANGELOG.md.
    assert package["changelog-path"] == "CHANGELOG.md"
    assert CONFIG["include-component-in-tag"] is True
    assert "include-v-in-tag" not in CONFIG  # the default, true, gives python-vX.Y.Z


def test_the_title_pattern_has_no_unknown_placeholders() -> None:
    assert "${" not in release_title("python", "0.1.0")


@pytest.mark.parametrize("component", ["python", "rust"])
def test_the_release_pull_request_passes_pr_hygiene(component: str) -> None:
    pr = PullRequest(
        title=release_title(component, "0.1.0"),
        branch=f"release-please--branches--main--components--{component}",
        body="",
        labels=frozenset({*extra_labels(), PENDING_LABEL}),
        milestone=None,
        author_is_bot=True,
    )
    assert pr.title == f"chore(release): release {component} 0.1.0"
    assert is_exempt_bot(pr)
    assert check(pr, {}) == []


def release_jobs() -> dict[str, str]:
    """Return the text of each job of release.yml, by its id."""
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    body = workflow[workflow.index("\njobs:\n") :]
    starts = list(re.finditer(r"^  ([a-z][a-z0-9-]*):\n", body, re.MULTILINE))
    ends = [match.start() for match in starts[1:]] + [len(body)]
    return {match.group(1): body[match.end() : end] for match, end in zip(starts, ends)}


JOBS = release_jobs()
SHARED = {"release-please", "test"}
# The jobs of each component: the crate's are named rust-* or publish to crates.io.
RUST_JOBS = {job for job in JOBS if job.startswith("rust-") or job == "publish-crates-io"}
PYTHON_JOBS = set(JOBS) - SHARED - RUST_JOBS


def test_every_job_belongs_to_a_component() -> None:
    assert {
        "build",
        "attest",
        "publish-testpypi",
        "smoke-testpypi",
        "publish-pypi",
        "github-release",
    } == PYTHON_JOBS
    assert {
        "rust-package",
        "rust-binaries",
        "rust-attest",
        "publish-crates-io",
        "rust-github-release",
    } == RUST_JOBS


def test_release_please_has_outputs_for_each_component() -> None:
    outputs = dict(
        re.findall(
            r"^      ([a-z-]+): \$\{\{ steps\.release\.outputs\['([^']+)'\] \}\}$",
            JOBS["release-please"],
            re.MULTILINE,
        )
    )
    assert outputs == {
        f"{component}-{name}": f"{component}--{key}"
        for component in ("python", "rust")
        for name, key in (
            ("release", "release_created"),
            ("tag", "tag_name"),
            ("version", "version"),
        )
    }
    assert "releases_created" not in "".join(JOBS.values())


@pytest.mark.parametrize(("component", "jobs"), [("python", PYTHON_JOBS), ("rust", RUST_JOBS)])
def test_each_component_s_jobs_read_only_its_own_release(component: str, jobs: set[str]) -> None:
    for job in jobs:
        used = set(re.findall(r"needs\.release-please\.outputs\.([a-z]+)-", JOBS[job]))
        assert used <= {component}, job


@pytest.mark.parametrize(("component", "first"), [("python", "build"), ("rust", "rust-package")])
def test_a_release_or_a_dry_run_starts_a_component(component: str, first: str) -> None:
    condition = JOBS[first].split("\n    runs-on:")[0]
    assert f"needs.release-please.outputs.{component}-release == 'true'" in condition
    assert "inputs.dry-run" in condition
    assert f"needs.release-please.outputs.{component}-release == 'true'" in JOBS["test"]


@pytest.mark.parametrize(
    ("job", "component", "environment"),
    [("publish-pypi", "python", "pypi"), ("publish-crates-io", "rust", "crates-io")],
)
def test_only_a_release_publishes_after_approval(
    job: str, component: str, environment: str
) -> None:
    text = JOBS[job]
    condition = text.split("\n    runs-on:")[0]
    assert f"&& needs.release-please.outputs.{component}-release == 'true'" in condition
    assert "dry-run" not in condition
    assert f"    environment:\n      name: {environment}\n" in text


def test_the_crates_io_token_is_only_in_its_environment() -> None:
    using = [job for job, text in JOBS.items() if "CARGO_REGISTRY_TOKEN" in text]
    assert using == ["publish-crates-io"]


def test_the_rust_artifacts_are_not_attached_to_the_python_release() -> None:
    uploaded = set(re.findall(r"^          name: (.+)$", "".join(JOBS.values()), re.MULTILINE))
    python = {name for name in uploaded if name.startswith("release-")}
    rust = {name for name in uploaded if name.startswith("rust-release-")}
    assert python == {"release-dist", "release-sbom", "release-attestations"}
    assert rust == {"rust-release-bin-${{ matrix.target }}", "rust-release-attestations"}
    assert "pattern: release-*" in JOBS["github-release"]
    assert "pattern: rust-release-*" in JOBS["rust-github-release"]


def test_the_release_labels_are_defined() -> None:
    text = (REPO / ".github" / "labels.yml").read_text(encoding="utf-8")
    defined = set(re.findall(r'^- name: "([^"]+)"$', text, re.MULTILINE))
    assert {*extra_labels(), PENDING_LABEL, TAGGED_LABEL} <= defined
