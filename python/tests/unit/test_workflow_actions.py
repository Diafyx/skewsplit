"""Checks that every workflow uses only actions the repository allows.

The Diafyx organization's Settings → Actions → General allows GitHub's own
actions and the patterns below, for every repository. GitHub refuses to start
a workflow that uses any other action, so the whole run fails before a job
starts. Keep ALLOWED equal to that setting.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

GITHUB = Path(__file__).parents[3] / ".github"
WORKFLOWS = sorted([*GITHUB.glob("workflows/*.yml"), *GITHUB.glob("actions/*/action.yml")])
# The owners whose actions are GitHub's own.
GITHUB_OWNED = ("actions", "github")
ALLOWED = (
    "astral-sh/setup-uv",
    "dorny/paths-filter",
    "re-actors/alls-green",
    "EndBug/label-sync",
    "amannn/action-semantic-pull-request",
    "googleapis/release-please-action",
    "pypa/gh-action-pypi-publish",
    "ossf/scorecard-action",
    "rust-lang/crates-io-auth-action",
)
_USES = re.compile(r"^\s*(?:- )?uses: ([^@\s]+)@([0-9a-f]{40}) # v\S+$", re.MULTILINE)


def actions(text: str) -> list[str]:
    """The actions a workflow uses, without local workflows; fail on an unpinned one."""
    used = [line.strip() for line in text.splitlines() if re.match(r"^\s*(?:- )?uses: ", line)]
    pinned = _USES.findall(text)
    remote = [line for line in used if "uses: ./" not in line]
    if len(pinned) != len(remote):
        msg = f"every action must be pinned to a commit SHA with a version comment: {remote}"
        raise ValueError(msg)
    return [name for name, _ in pinned]


def allowed(action: str) -> bool:
    owner = action.split("/", 1)[0]
    repository = "/".join(action.split("/", 2)[:2])
    return owner in GITHUB_OWNED or repository in ALLOWED


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_every_action_is_allowed(workflow: Path) -> None:
    used = actions(workflow.read_text(encoding="utf-8"))
    assert [action for action in used if not allowed(action)] == []


def test_the_workflows_are_found() -> None:
    assert {path.name for path in WORKFLOWS} >= {"ci.yml", "release.yml", "nightly.yml"}


def test_an_action_of_another_owner_is_refused() -> None:
    text = "      - uses: taiki-e/install-action@" + "a" * 40 + " # v2.90.5\n"
    assert actions(text) == ["taiki-e/install-action"]
    assert not allowed("taiki-e/install-action")
    assert allowed("github/codeql-action/init")
    assert allowed("actions/cache")


def test_an_unpinned_action_is_an_error() -> None:
    with pytest.raises(ValueError, match="pinned to a commit SHA"):
        actions("      - uses: actions/cache@v6\n")


def test_a_local_workflow_is_not_an_action() -> None:
    assert actions("    uses: ./.github/workflows/ci.yml # zizmor: ignore[x]\n") == []
