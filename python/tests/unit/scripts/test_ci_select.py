from __future__ import annotations

import re
from pathlib import Path

import pytest

from ci_select import ALWAYS, JOBS, NOT_GATED, PR_ONLY, main, render, select

WORKFLOW = Path(__file__).parents[4] / ".github" / "workflows" / "ci.yml"

ALL_JOBS = {
    "lint",
    "pr-size",
    "py-unit",
    "py-lowest",
    "coverage",
    "packaging",
    "docs",
    "conformance",
    "core-bench",
}
PYTHON_JOBS = {"py-unit", "py-lowest", "coverage", "packaging", "conformance"}


def running(selected: dict[str, bool]) -> set[str]:
    return {job for job, run in selected.items() if run}


def test_docs_only_pull_request_runs_lint_docs_and_pr_size() -> None:
    assert running(select("pull_request", {"docs"}, set(), full=False)) == {
        "lint",
        "docs",
        "pr-size",
    }


def test_benchmark_changes_run_the_unit_tests_of_its_helpers() -> None:
    assert running(select("pull_request", {"bench"}, set(), full=False)) == {
        "lint",
        "py-unit",
        "coverage",
        "pr-size",
    }


def test_repository_housekeeping_runs_lint_and_pr_size_only() -> None:
    assert running(select("pull_request", {"repo"}, set(), full=False)) == {"lint", "pr-size"}


@pytest.mark.parametrize(
    ("group", "extra"), [("py-core", {"core-bench"}), ("py-adapters", set()), ("cli", set())]
)
def test_python_changes_run_the_python_jobs_and_docs(group: str, extra: set[str]) -> None:
    assert running(select("pull_request", {group}, set(), full=False)) == (
        PYTHON_JOBS | {"lint", "docs", "pr-size"} | extra
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
    text = render({"lint": True, "docs": False, "py-unit": False, "core-bench": False})
    assert text == (
        "lint=true\ndocs=false\npy-unit=false\ncore-bench=false\nallowed-skips=docs,py-unit\n"
    )


def test_main_appends_to_github_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    output.write_text("earlier=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert main(["--event", "pull_request", "--groups", '["repo"]']) == 0
    written = output.read_text(encoding="utf-8")
    assert written.startswith("earlier=1\nlint=true\n")
    assert "allowed-skips=py-unit,py-lowest,coverage,packaging,docs,conformance\n" in written
    assert capsys.readouterr().out == written.removeprefix("earlier=1\n")


def test_main_prints_without_github_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert main(["--event", "push"]) == 0
    assert "allowed-skips=pr-size\n" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["1001", '{"a": 1}', '["ok", 2]'])
def test_main_rejects_values_that_are_not_lists_of_strings(value: str) -> None:
    with pytest.raises(TypeError, match="--groups must be a JSON list of strings"):
        main(["--event", "pull_request", "--groups", value])


def test_ci_workflow_has_every_selected_job_and_gates_all_but_the_ungated() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    outputs = set(
        re.findall(r"^      ([a-z-]+): \$\{\{ steps\.select\.outputs\.\1 \}\}$", text, re.MULTILINE)
    )
    jobs = set(re.findall(r"^  ([a-z-]+):$", text, re.MULTILINE))
    gate = re.search(r"^  ci-gate:\n(?:    .*\n)*?    needs: \[(.*)\]$", text, re.MULTILINE)
    assert gate is not None
    assert outputs == {*JOBS, "allowed-skips"}
    assert set(JOBS) | set(ALWAYS) | set(PR_ONLY) <= jobs
    assert set(gate.group(1).split(", ")) == {"changes", *ALWAYS, *PR_ONLY, *JOBS} - NOT_GATED
