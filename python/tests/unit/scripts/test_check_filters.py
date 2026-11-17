from __future__ import annotations

from pathlib import Path

import pytest

import check_filters
import ci_select
from check_filters import glob_to_regex, parse_filters, unmatched

REPO = Path(__file__).parents[4]
FILTERS = REPO / ".github" / "filters.yml"


def test_parse_reads_groups_and_quoted_or_bare_patterns() -> None:
    text = "# comment\n\nspec:\n  - 'spec/**'\n  # inner comment\nci:\n  - .github/**\n"
    assert parse_filters(text) == {"spec": ["spec/**"], "ci": [".github/**"]}


@pytest.mark.parametrize(
    "text",
    [
        "shared: &shared\n  - 'a/**'\n",  # anchors
        "spec: ['spec/**']\n",  # inline lists
        "  - 'orphan/**'\n",  # item before any group
        "spec:\n    nested:\n",  # nesting
    ],
)
def test_parse_rejects_unsupported_syntax(text: str) -> None:
    with pytest.raises(ValueError, match="unsupported syntax"):
        parse_filters(text)


def test_parse_rejects_empty_groups() -> None:
    with pytest.raises(ValueError, match="groups without patterns: docs"):
        parse_filters("docs:\nspec:\n  - 'spec/**'\n")


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("python/**", "python/src/skewsplit/cli.py", True),
        ("python/**", "pythonic/x.py", False),
        ("**/*.md", "README.md", True),
        ("**/*.md", "docs/adr/0001-x.md", True),
        ("**/*.md", "docs/adr/0001-x.mdx", False),
        ("*.toml", "pyproject.toml", True),
        ("*.toml", "python/pyproject.toml", False),
        ("Makefile", "Makefile", True),
        ("Makefile", "python/Makefile", False),
        (".github/**", ".github/workflows/ci.yml", True),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file/.txt", False),
        ("a.b", "aXb", False),
    ],
)
def test_globs_follow_picomatch(pattern: str, path: str, *, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).fullmatch(path)) is expected


@pytest.mark.parametrize("pattern", ["src/[ab]/**", "src/{a,b}/**", "!docs/**"])
def test_globs_reject_syntax_the_checker_cannot_mirror(pattern: str) -> None:
    with pytest.raises(ValueError, match="unsupported glob"):
        glob_to_regex(pattern)


def test_unmatched_reports_a_file_that_matches_no_group() -> None:
    groups = {"py": ["python/**"], "docs": ["**/*.md"]}
    files = ["python/a.py", "README.md", "newfolder/thing.txt"]
    assert unmatched(files, groups) == ["newfolder/thing.txt"]


def test_main_fails_on_a_file_that_matches_no_group(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    filters = tmp_path / "filters.yml"
    filters.write_text("py:\n  - 'python/**'\n", encoding="utf-8")
    monkeypatch.setattr(check_filters, "tracked_files", lambda: ["python/a.py", "orphan.txt"])
    assert check_filters.main([str(filters)]) == 1
    assert "orphan.txt matches no group" in capsys.readouterr().err


def test_every_tracked_file_in_this_repository_matches_a_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO)
    assert check_filters.main([]) == 0


def test_filter_groups_are_the_ones_ci_select_knows() -> None:
    assert set(parse_filters(FILTERS.read_text(encoding="utf-8"))) == ci_select.GROUPS
