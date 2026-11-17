from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ci_select import main, render, select

if TYPE_CHECKING:
    from pathlib import Path

ALL_JOBS = {"lint", "pr-size", "py-unit", "py-lowest", "coverage", "packaging", "docs"}
PYTHON_JOBS = {"py-unit", "py-lowest", "coverage", "packaging"}


def running(selected: dict[str, bool]) -> set[str]:
    return {job for job, run in selected.items() if run}


def test_docs_only_pull_request_runs_lint_docs_and_pr_size() -> None:
    assert running(select("pull_request", {"docs"}, set(), full=False)) == {
        "lint",
        "docs",
        "pr-size",
    }


def test_repository_housekeeping_runs_lint_and_pr_size_only() -> None:
    assert running(select("pull_request", {"repo"}, set(), full=False)) == {"lint", "pr-size"}


@pytest.mark.parametrize("group", ["py-core", "py-adapters", "cli"])
def test_python_changes_run_the_python_jobs_and_docs(group: str) -> None:
    assert running(select("pull_request", {group}, set(), full=False)) == (
        PYTHON_JOBS | {"lint", "docs", "pr-size"}
    )


@pytest.mark.parametrize("group", ["spec", "ci", "build"])
def test_shared_inputs_run_every_job(group: str) -> None:
    assert running(select("pull_request", {group}, set(), full=False)) == ALL_JOBS


def test_ci_full_label_runs_every_job() -> None:
    assert running(select("pull_request", {"docs"}, {"ci:full"}, full=False)) == ALL_JOBS


def test_full_flag_runs_every_job() -> None:
    assert running(select("pull_request", set(), set(), full=True)) == ALL_JOBS


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_other_events_run_everything_except_pr_size(event: str) -> None:
    assert running(select(event, set(), set(), full=False)) == ALL_JOBS - {"pr-size"}


def test_unknown_groups_are_an_error() -> None:
    with pytest.raises(ValueError, match="unknown filter groups: nope"):
        select("pull_request", {"nope"}, set(), full=False)


def test_render_lists_jobs_and_the_skips_the_gate_may_accept() -> None:
    text = render({"lint": True, "docs": False, "py-unit": False})
    assert text == "lint=true\ndocs=false\npy-unit=false\nallowed-skips=docs,py-unit\n"


def test_main_appends_to_github_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.write_text("earlier=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main(["--event", "pull_request", "--groups", '["repo"]']) == 0
    written = output.read_text(encoding="utf-8")
    assert written.startswith("earlier=1\nlint=true\n")
    assert "allowed-skips=py-unit,py-lowest,coverage,packaging,docs\n" in written
    assert capsys.readouterr().out == written.removeprefix("earlier=1\n")


def test_main_prints_without_github_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert main(["--event", "push"]) == 0
    assert "allowed-skips=pr-size\n" in capsys.readouterr().out
