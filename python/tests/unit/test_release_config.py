"""Checks that the release-please setup agrees with the package and the PR rules.

The release pull request changes the manifest, python/pyproject.toml and
uv.lock together, so CI on that pull request runs these checks too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

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


def test_the_python_component_is_tagged_python_v() -> None:
    package = CONFIG["packages"]["python"]
    assert package["component"] == "python"
    assert CONFIG["include-component-in-tag"] is True
    assert "include-v-in-tag" not in CONFIG  # the default, true, gives python-vX.Y.Z


def test_the_title_pattern_has_no_unknown_placeholders() -> None:
    assert "${" not in release_title("python", "0.1.0")


def test_the_release_pull_request_passes_pr_hygiene() -> None:
    pr = PullRequest(
        title=release_title("python", "0.1.0"),
        branch="release-please--branches--main--components--python",
        body="",
        labels=frozenset({*extra_labels(), PENDING_LABEL}),
        milestone=None,
        author_is_bot=True,
    )
    assert pr.title == "chore(release): release python 0.1.0"
    assert is_exempt_bot(pr)
    assert check(pr, {}) == []


def test_the_release_labels_are_defined() -> None:
    text = (REPO / ".github" / "labels.yml").read_text(encoding="utf-8")
    defined = set(re.findall(r'^- name: "([^"]+)"$', text, re.MULTILINE))
    assert {*extra_labels(), PENDING_LABEL, TAGGED_LABEL} <= defined
