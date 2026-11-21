"""Checks on the repository's Markdown files that a linter cannot make."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
_BLOCK = re.compile(
    r"<!-- supported-versions:start -->\n(.*?)<!-- supported-versions:end -->", re.DOTALL
)


def supported_versions(text: str) -> str:
    blocks = _BLOCK.findall(text)
    if len(blocks) != 1:
        msg = f"expected one supported-versions block, found {len(blocks)}"
        raise ValueError(msg)
    return str(blocks[0])


def test_readme_and_security_list_the_same_supported_versions() -> None:
    readme = supported_versions((REPO / "README.md").read_text(encoding="utf-8"))
    security = supported_versions((REPO / "SECURITY.md").read_text(encoding="utf-8"))
    assert readme == security


@pytest.mark.parametrize("count", [0, 2])
def test_supported_versions_needs_exactly_one_block(count: int) -> None:
    block = "<!-- supported-versions:start -->\nx\n<!-- supported-versions:end -->\n"
    with pytest.raises(ValueError, match=f"found {count}"):
        supported_versions(block * count)


def test_claude_md_only_imports_agents_md() -> None:
    assert (REPO / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"


def fenced(text: str, info: str) -> list[str]:
    """Return the contents of the fenced code blocks with this info string."""
    return re.findall(rf"^```{info}\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)


# The repository's Markdown: the top-level files, the package README, and the
# folders that hold docs. Local folders, such as virtual environments, are not
# searched.
MARKDOWN = sorted(
    [
        *REPO.glob("*.md"),
        REPO / "python" / "README.md",
        *(
            path
            for folder in (".github", "bench", "docs", "spec")
            for path in (REPO / folder).rglob("*.md")
        ),
    ]
)


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda path: str(path.relative_to(REPO)))
def test_python_blocks_without_prompts_compile(path: Path) -> None:
    # The docs job runs the >>> examples; the others need a database or Spark.
    for block in fenced(path.read_text(encoding="utf-8"), "python"):
        if not block.startswith(">>> "):
            compile(block, str(path), "exec")


def test_the_readme_cli_example_prints_the_quick_start_report() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    report = re.search(r"^>>> print\(plan\.report\)\n(.*?)^>>> ", readme, re.MULTILINE | re.DOTALL)
    cli = [block for block in fenced(readme, "text") if block.startswith("$ skewsplit plan ")]
    assert report is not None
    assert len(cli) == 1
    assert cli[0].split("\n", 1)[1] == report.group(1)


def test_fenced_finds_blocks_by_info_string() -> None:
    text = "```python\na = 1\n```\n\n```text\nb\n```\n```python\n>>> c\n```\n"
    assert fenced(text, "python") == ["a = 1\n", ">>> c\n"]
    assert fenced(text, "text") == ["b\n"]
