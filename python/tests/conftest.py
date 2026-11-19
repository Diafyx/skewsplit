"""Shared pytest configuration.

Every test gets its kind marker from the folder it lives in, so a plain
``pytest`` (which selects ``unit or property``) can never skip a test by
accident because someone forgot a marker.

Hypothesis reads its profile from ``HYPOTHESIS_PROFILE``:

- ``dev`` (the default): quick, for local runs and the py-unit matrix.
- ``ci``: ten times the examples, for the conformance job.
- ``nightly``: a long search, for the scheduled run.

No profile has a deadline: timings on shared CI runners vary too much to fail
a test on, and the benchmarks measure speed instead. ``ci`` and ``nightly``
print the blob that replays a failure with ``@reproduce_failure``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import settings

TESTS = Path(__file__).parent
KINDS = ("unit", "property", "integration", "spark")

settings.register_profile("dev", max_examples=100, deadline=None)
settings.register_profile("ci", max_examples=1000, deadline=None, print_blob=True)
settings.register_profile("nightly", max_examples=20000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        kind = item.path.relative_to(TESTS).parts[0]
        if kind not in KINDS:
            msg = f"{item.nodeid}: tests must live in one of {', '.join(KINDS)}/"
            raise pytest.UsageError(msg)
        item.add_marker(kind)
