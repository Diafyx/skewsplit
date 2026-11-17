"""Shared pytest configuration.

Every test gets its kind marker from the folder it lives in, so a plain
``pytest`` (which selects ``unit or property``) can never skip a test by
accident because someone forgot a marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TESTS = Path(__file__).parent
KINDS = ("unit", "property", "integration", "spark")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        kind = item.path.relative_to(TESTS).parts[0]
        if kind not in KINDS:
            msg = f"{item.nodeid}: tests must live in one of {', '.join(KINDS)}/"
            raise pytest.UsageError(msg)
        item.add_marker(kind)
