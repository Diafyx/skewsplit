from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

import pr_size
from pr_size import parse_check_attr, parse_numstat, size_label, why_large_filled

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("lines", "label"),
    [
        (0, "size/S"),
        (100, "size/S"),
        (101, "size/M"),
        (300, "size/M"),
        (301, "size/L"),
        (700, "size/L"),
        (701, "size/XL"),
        (1500, "size/XL"),
        (1501, "size/XXL"),
    ],
)
def test_size_label_boundaries(lines: int, label: str) -> None:
    assert size_label(lines) == label


def test_numstat_counts_added_and_deleted_and_binary_as_zero() -> None:
    numstat = "10\t2\tMakefile\0-\t-\tlogo.png\0001\t0\tpath with\ttab.txt\0"
    assert parse_numstat(numstat) == {"Makefile": 12, "logo.png": 0, "path with\ttab.txt": 1}


def test_check_attr_keeps_only_generated_paths() -> None:
    output = "uv.lock\0linguist-generated\0set\0a.py\0linguist-generated\0unspecified\0"
    output += "f.json\0linguist-generated\0true\0b.py\0linguist-generated\0false\0"
    assert parse_check_attr(output) == {"uv.lock", "f.json"}


BODY = """### Why is this PR large?
<!--
Required only if CI labels this PR size/XXL.
-->
{answer}

### Checklist
- [ ] something
"""


@pytest.mark.parametrize("answer", ["", "N/A", "n/a.", "No", "none"])
def test_why_large_is_empty_for_placeholders(answer: str) -> None:
    assert not why_large_filled(BODY.format(answer=answer))


def test_why_large_is_filled_with_a_real_reason() -> None:
    assert why_large_filled(BODY.format(answer="The generated parser cannot be split."))


def test_why_large_is_empty_when_the_heading_is_missing() -> None:
    assert not why_large_filled("### Checklist\n- [ ] x\n")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / ".gitattributes").write_text("*.lock linguist-generated=true\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def commit_lines(repo: Path, **files: int) -> None:
    for name, count in files.items():
        (repo / name.replace("_", ".")).write_text("x\n" * count)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")


def test_changed_lines_excludes_generated_files(repo: Path) -> None:
    commit_lines(repo, a_py=40, uv_lock=5000)
    assert pr_size.changed_lines("main~1", "HEAD") == 40


@pytest.mark.usefixtures("repo")
def test_changed_lines_is_zero_without_changes() -> None:
    assert pr_size.changed_lines("HEAD", "HEAD") == 0


def test_main_writes_the_label_to_github_output(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commit_lines(repo, a_py=150)
    output = repo / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert pr_size.main(["main~1", "HEAD"]) == 0
    assert output.read_text() == "label=size/M\n"
    assert "150 changed lines -> size/M" in capsys.readouterr().out


def test_main_fails_xxl_without_a_reason(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    commit_lines(repo, a_py=1600)
    body = repo / "body.md"
    body.write_text(BODY.format(answer="N/A"))
    assert pr_size.main(["main~1", "HEAD", str(body)]) == 1
    assert "size/XXL needs a filled-in" in capsys.readouterr().err


def test_main_accepts_xxl_with_a_reason(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    commit_lines(repo, a_py=1600)
    body = repo / "body.md"
    body.write_text(BODY.format(answer="One generated table that must land at once."))
    assert pr_size.main(["main~1", "HEAD", str(body)]) == 0
