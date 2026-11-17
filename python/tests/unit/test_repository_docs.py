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
