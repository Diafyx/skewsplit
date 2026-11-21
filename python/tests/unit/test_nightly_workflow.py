"""The scheduled workflow grants issue access only where it opens an issue."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
NIGHTLY = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
_JOB = re.compile(r"^  ([a-z][a-z0-9-]*):\n", re.MULTILINE)


def jobs(text: str) -> dict[str, str]:
    """Return the text of each job, by its id."""
    body = text[text.index("\njobs:\n") :]
    starts = list(_JOB.finditer(body))
    ends = [match.start() for match in starts[1:]] + [len(body)]
    return {match.group(1): body[match.end() : end] for match, end in zip(starts, ends)}


def test_the_workflow_grants_nothing_by_default() -> None:
    assert "\npermissions: {}\n" in NIGHTLY


def test_every_job_sets_its_own_permissions() -> None:
    found = jobs(NIGHTLY)
    assert set(found) == {"ci", "hypothesis", "fuzz", "mutation", "mutation-issue", "links"}
    for job, text in found.items():
        assert "\n    permissions:\n      contents: read\n" in f"\n{text}", job


def test_only_the_jobs_that_open_issues_can_write_them() -> None:
    for job, text in jobs(NIGHTLY).items():
        opens = "scripts/tracking_issue.py" in text
        assert ("      issues: write" in text) == opens, job
        assert opens == (job == "mutation-issue")


def test_the_dry_run_input_reaches_the_issue_job() -> None:
    issue = jobs(NIGHTLY)["mutation-issue"]
    assert "needs.mutation.result == 'failure' || inputs.dry-run" in issue
    assert "DRY_RUN: ${{ inputs.dry-run && 'true' || 'false' }}" in issue
    assert 'if [[ "$DRY_RUN" == true ]]; then args+=(--dry-run); fi' in issue


def test_the_issue_labels_exist() -> None:
    text = (ROOT / ".github" / "labels.yml").read_text(encoding="utf-8")
    labels = set(re.findall(r'^- name: "([^"]+)"$', text, re.MULTILINE))
    used = re.findall(r"--label (\S+)", jobs(NIGHTLY)["mutation-issue"])
    assert used == ["type/test", "area/core", "status/triage"]
    assert set(used) <= labels


def test_jobs_finds_each_job() -> None:
    text = "on: push\njobs:\n  one:\n    runs-on: x\n  two-2:\n    steps: []\n"
    assert jobs(text) == {"one": "    runs-on: x\n", "two-2": "    steps: []\n"}
