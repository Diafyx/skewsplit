"""Container images in the workflows are pinned by digest.

zizmor checks the images it can read, but not those a job takes from its
matrix, such as the PostgreSQL versions of py-integration.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).parents[3] / ".github" / "workflows").glob("*.yml"))
_IMAGE = re.compile(r"^\s*(?:- )?image: (.+)$", re.MULTILINE)
_PINNED = re.compile(r"[a-z0-9./-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}")
_FROM_MATRIX = "${{ matrix.image }}"


def unpinned(text: str) -> list[str]:
    """Return the image values that are neither pinned nor taken from the matrix."""
    images = _IMAGE.findall(text)
    return [image for image in images if image != _FROM_MATRIX and not _PINNED.fullmatch(image)]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_images_are_pinned_by_digest(workflow: Path) -> None:
    assert unpinned(workflow.read_text(encoding="utf-8")) == []


def test_the_postgres_images_are_found() -> None:
    images = _IMAGE.findall((WORKFLOWS[0].parent / "ci.yml").read_text(encoding="utf-8"))
    assert sum(image.startswith("postgres:") for image in images) == 7


def test_unpinned_images_are_reported() -> None:
    digest = "0" * 64
    text = (
        f"    image: postgres:18.7@sha256:{digest}\n"
        "    image: postgres:18.7\n"
        "            image: postgres:latest@sha256:abc\n"
        "  - image: ${{ matrix.image }}\n"
        "          - image: postgres\n"
    )
    assert unpinned(text) == ["postgres:18.7", "postgres:latest@sha256:abc", "postgres"]
