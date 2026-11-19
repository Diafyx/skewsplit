"""Checks that Dependabot's pull requests will pass pr-hygiene.

`make lint` validates .github/dependabot.yml against its schema. These tests
read the commit prefixes, groups and labels from it and build the pull
requests Dependabot would open from them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from pr_hygiene import PullRequest, check, is_exempt_bot

REPO = Path(__file__).parents[3]
TEXT = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Update:
    """The parts of one `updates` entry that shape a pull request."""

    ecosystem: str
    prefixes: tuple[str, ...]
    groups: tuple[str, ...]
    labels: frozenset[str]
    ignored: frozenset[str]


def parse_updates(text: str) -> list[Update]:
    """Read the updates from the file's fixed layout; fail if it changed."""
    updates = []
    for block in re.split(r"^  - package-ecosystem: ", text, flags=re.MULTILINE)[1:]:
        ecosystem = block.split("\n", 1)[0].strip('"')
        prefixes = re.findall(r'^      prefix(?:-development)?: "([^"]+)"$', block, re.MULTILINE)
        groups = re.findall(r"^      ([a-z-]+):\n        update-types:", block, re.MULTILINE)
        labels = re.search(r'^    labels:\n((?:      - "[^"]+"\n)+)', block, re.MULTILINE)
        if not (prefixes and groups and labels):
            msg = f"cannot read the {ecosystem} entry; update this parser with the file"
            raise ValueError(msg)
        updates.append(
            Update(
                ecosystem=ecosystem,
                prefixes=tuple(prefixes),
                groups=tuple(groups),
                labels=frozenset(re.findall(r'"([^"]+)"', labels.group(1))),
                ignored=frozenset(
                    re.findall(r'^      - dependency-name: "([^"]+)"$', block, re.MULTILINE)
                ),
            )
        )
    return updates


def split_requirements(pyproject: str) -> set[str]:
    """Return the dependency-group packages listed with more than one range."""
    after = re.split(r"^\[dependency-groups\]$", pyproject, maxsplit=1, flags=re.MULTILINE)[1]
    groups = re.split(r"^\[", after, maxsplit=1, flags=re.MULTILINE)[0]
    names = [
        re.sub(r"[-_.]+", "-", name.lower())
        for name in re.findall(r'^  "([A-Za-z0-9][A-Za-z0-9_.-]*)', groups, re.MULTILINE)
    ]
    return {name for name in names if names.count(name) > 1}


UPDATES = parse_updates(TEXT)


def test_every_ecosystem_is_configured() -> None:
    assert [update.ecosystem for update in UPDATES] == ["github-actions", "uv", "pip"]


def test_an_unreadable_entry_is_an_error() -> None:
    with pytest.raises(ValueError, match="cannot read the uv entry"):
        parse_updates('updates:\n  - package-ecosystem: "uv"\n    directory: "/"\n')


def test_uv_ignores_exactly_the_split_packages() -> None:
    # Dependabot cannot update a package locked at several versions, and an
    # ignore left behind after a split is removed would hide updates.
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    uv = next(update for update in UPDATES if update.ecosystem == "uv")
    assert uv.ignored == split_requirements(pyproject)


def test_split_requirements_finds_repeated_names_only() -> None:
    pyproject = (
        "[dependency-groups]\n"
        "test = [\n"
        "  \"Foo_Bar[x]>=2; python_version>='3.10'\",\n"
        "  \"foo-bar>=1,<2; python_version<'3.10'\",\n"
        '  "single>=1",\n'
        "]\n"
        'dev = [{ include-group = "test" }]\n'
        "\n[tool.other]\n"
        'x = [\n  "single>=2",\n]\n'
    )
    assert split_requirements(pyproject) == {"foo-bar"}


@pytest.mark.parametrize("update", UPDATES, ids=lambda update: update.ecosystem)
def test_dependabot_pull_requests_pass_pr_hygiene(update: Update) -> None:
    summaries = [
        "bump example from 1.0.0 to 2.0.0",
        *(f"bump the {group} group across 1 directory with 3 updates" for group in update.groups),
    ]
    for prefix in update.prefixes:
        for summary in summaries:
            pr = PullRequest(
                # Dependabot puts a colon after a prefix that ends with ")".
                title=f"{prefix}: {summary}",
                branch=f"dependabot/{update.ecosystem}/example-2.0.0",
                body="",
                labels=update.labels,
                milestone=None,
                author_is_bot=True,
            )
            assert is_exempt_bot(pr)
            assert check(pr, {}) == [], pr.title


@pytest.mark.parametrize("update", UPDATES, ids=lambda update: update.ecosystem)
def test_dependabot_labels_are_defined(update: Update) -> None:
    text = (REPO / ".github" / "labels.yml").read_text(encoding="utf-8")
    defined = set(re.findall(r'^- name: "([^"]+)"$', text, re.MULTILINE))
    assert update.labels <= defined
