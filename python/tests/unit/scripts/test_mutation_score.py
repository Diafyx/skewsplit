from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mutation_score import main, score, summary

if TYPE_CHECKING:
    from pathlib import Path

STATS = {
    "killed": 1818,
    "survived": 162,
    "total": 1985,
    "no_tests": 0,
    "skipped": 0,
    "suspicious": 0,
    "timeout": 5,
    "check_was_interrupted_by_user": 0,
    "segfault": 0,
}


def counts(**changes: int) -> dict[str, int]:
    return {**STATS, **changes}


def test_the_score_counts_killed_mutants_only() -> None:
    assert score(counts()) == pytest.approx(100 * 1818 / 1985)


def test_skipped_mutants_are_not_tested() -> None:
    assert score(counts(killed=90, total=110, skipped=10)) == 90


def test_no_tested_mutant_has_no_score() -> None:
    assert score(counts(killed=0, total=3, skipped=3)) is None
    assert score(counts(killed=0, total=0)) is None


def test_summary_lists_the_counts() -> None:
    text = summary(counts(), 91.6, 90)
    assert "Score: 91.6% of the tested mutants killed (minimum 90%): passes." in text
    assert "| killed | 1818 |\n| survived | 162 |\n| timeout | 5 |" in text
    assert "| no tests | 0 |" in text
    assert text.endswith("| total | 1985 |\n")


def test_summary_without_a_score_fails() -> None:
    assert "Score: no mutant was tested (minimum 90%): fails." in summary(counts(), None, 90)


@pytest.fixture
def github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point $GITHUB_OUTPUT and $GITHUB_STEP_SUMMARY at two files."""
    output, step_summary = tmp_path / "output", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    return output, step_summary


def write(tmp_path: Path, stats: dict[str, int]) -> Path:
    path = tmp_path / "mutation.json"
    path.write_text(json.dumps(stats), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("killed", "code", "shown"), [(90, 0, "90.0"), (89, 1, "89.0"), (100, 0, "100.0")]
)
def test_main_fails_below_the_minimum(
    killed: int,
    code: int,
    shown: str,
    tmp_path: Path,
    github: tuple[Path, Path],
) -> None:
    output, step_summary = github
    stats = write(tmp_path, counts(killed=killed, survived=100 - killed, total=100, timeout=0))
    assert main(["--min", "90", str(stats)]) == code
    printed = step_summary.read_text(encoding="utf-8")
    assert f"Score: {shown}% " in printed
    assert output.read_text(encoding="utf-8") == f"score={shown}\n"


def test_main_fails_when_no_mutant_was_tested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main(["--min", "90", str(write(tmp_path, counts(killed=0, total=0)))]) == 1
    assert "no mutant was tested" in capsys.readouterr().out
    assert output.read_text(encoding="utf-8") == "score=\n"


def test_main_needs_every_count(tmp_path: Path) -> None:
    stats = dict(STATS)
    del stats["timeout"]
    with pytest.raises(KeyError, match="timeout"):
        main(["--min", "90", str(write(tmp_path, stats))])
