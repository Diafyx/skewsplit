"""The scheduled workflows grant issue access only where they open an issue."""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
_JOB = re.compile(r"^  ([a-z][a-z0-9-]*):\n", re.MULTILINE)


class Scheduled(NamedTuple):
    """A scheduled workflow, its jobs, and the jobs that open its issues, with their labels."""

    file: str
    jobs: set[str]
    issue_jobs: dict[str, list[str]]


SCHEDULED = [
    Scheduled(
        "nightly.yml",
        {
            "ci",
            "hypothesis",
            "fuzz",
            "rust-fuzz",
            "differential",
            "mutation",
            "mutation-issue",
            "bench",
            "bench-issue",
            "links",
        },
        {
            "mutation-issue": ["type/test", "area/core", "status/triage"],
            "bench-issue": ["type/perf", "area/bench", "status/triage"],
        },
    ),
    Scheduled(
        "canary.yml",
        {
            "resolve",
            "python-next",
            "pyspark-next",
            "postgres-next",
            "pgjdbc-next",
            "canary-issue",
        },
        {"canary-issue": ["type/ci", "area/ci", "status/triage"]},
    ),
]


def jobs(text: str) -> dict[str, str]:
    """Return the text of each job, by its id."""
    body = text[text.index("\njobs:\n") :]
    starts = list(_JOB.finditer(body))
    ends = [match.start() for match in starts[1:]] + [len(body)]
    return {match.group(1): body[match.end() : end] for match, end in zip(starts, ends)}


def read(workflow: Scheduled) -> str:
    return (WORKFLOWS / workflow.file).read_text(encoding="utf-8")


parametrize = pytest.mark.parametrize("workflow", SCHEDULED, ids=lambda w: w.file)


@parametrize
def test_the_workflow_runs_on_a_schedule_and_by_hand(workflow: Scheduled) -> None:
    text = read(workflow)
    assert re.search(
        r'^on:\n  schedule:\n    - cron: "[0-9 *]+"\n  workflow_dispatch:\n', text, re.MULTILINE
    )


@parametrize
def test_the_workflow_grants_nothing_by_default(workflow: Scheduled) -> None:
    assert "\npermissions: {}\n" in read(workflow)


@parametrize
def test_every_job_sets_its_own_permissions(workflow: Scheduled) -> None:
    found = jobs(read(workflow))
    assert set(found) == workflow.jobs
    for job, text in found.items():
        assert "\n    permissions:\n      contents: read\n" in f"\n{text}", job


@parametrize
def test_only_the_job_that_opens_issues_can_write_them(workflow: Scheduled) -> None:
    for job, text in jobs(read(workflow)).items():
        opens = "scripts/tracking_issue.py" in text
        assert ("      issues: write" in text) == opens, job
        assert opens == (job in workflow.issue_jobs), job


@parametrize
def test_the_dry_run_input_reaches_the_issue_job(workflow: Scheduled) -> None:
    for name in workflow.issue_jobs:
        issue = jobs(read(workflow))[name]
        # On the job, or on the step that opens the issue.
        assert re.search(r"    if: \$\{\{ [^\n]* \|\| inputs\.dry-run\)? \}\}\n", issue), name
        assert "DRY_RUN: ${{ inputs.dry-run && 'true' || 'false' }}" in issue, name
        assert 'if [[ "$DRY_RUN" == true ]]; then args+=(--dry-run); fi' in issue, name


@parametrize
def test_the_issue_labels_exist(workflow: Scheduled) -> None:
    text = (ROOT / ".github" / "labels.yml").read_text(encoding="utf-8")
    labels = set(re.findall(r'^- name: "([^"]+)"$', text, re.MULTILINE))
    for name, expected in workflow.issue_jobs.items():
        used = re.findall(r"--label (\S+)", jobs(read(workflow))[name])
        assert used == expected, name
        assert set(used) <= labels, name


def test_the_canary_issue_waits_for_every_job() -> None:
    issue = jobs(read(SCHEDULED[1]))["canary-issue"]
    needs = re.search(r"^    needs: \[(.*)\]$", issue, re.MULTILINE)
    assert needs is not None
    assert set(needs.group(1).split(", ")) == SCHEDULED[1].jobs - {"canary-issue"}
    assert "contains(needs.*.result, 'failure')" in issue


def test_each_canary_job_runs_only_when_resolve_found_its_release() -> None:
    found = jobs(read(SCHEDULED[1]))
    outputs = {
        "python-next": "python",
        "pyspark-next": "pyspark",
        "postgres-next": "postgres-image",
        "pgjdbc-next": "pgjdbc",
    }
    for job, output in outputs.items():
        assert f"    if: needs.resolve.outputs.{output} != ''\n" in found[job], job


def test_jobs_finds_each_job() -> None:
    text = "on: push\njobs:\n  one:\n    runs-on: x\n  two-2:\n    steps: []\n"
    assert jobs(text) == {"one": "    runs-on: x\n", "two-2": "    steps: []\n"}


def test_the_postgres_canary_refuses_an_image_that_is_not_pinned() -> None:
    job = jobs(read(SCHEDULED[1]))["postgres-next"]
    assert "services:" not in job
    check = '[[ "$IMAGE" =~ ^postgres:${POSTGRES}(beta|rc)[0-9]+@sha256:[0-9a-f]{64}$ ]]'
    assert job.index(check) < job.index("docker run ")
