"""make mutation runs mutmut in an environment that has the test tools."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[4]
_MUTMUT = re.compile(r"^MUTMUT := cd python && \$\(UV_RUN\) (.*) mutmut$", re.MULTILINE)
_INSTALL = re.compile(r"^ \+ ([A-Za-z0-9_.-]+)==", re.MULTILINE)


def mutmut_flags() -> list[str]:
    match = _MUTMUT.search((ROOT / "Makefile").read_text(encoding="utf-8"))
    assert match is not None
    return match.group(1).split()


def test_make_mutation_installs_the_test_tools_from_the_package_folder(tmp_path: Path) -> None:
    # A dry run from the lockfile into an empty path: it installs and downloads
    # nothing, and lists what a fresh environment would get, as in CI.
    uv = shutil.which("uv")
    assert uv is not None
    done = subprocess.run(
        [uv, "sync", "--frozen", "--dry-run", "--python", sys.executable, *mutmut_flags()],
        cwd=ROOT / "python",
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    installed = set(_INSTALL.findall(done.stdout + done.stderr))
    assert {"pytest", "pytest-benchmark", "hypothesis", "psycopg"} <= installed
    # mutmut needs Python 3.10, and its group installs it from 3.11.
    assert ("mutmut" in installed) == (sys.version_info >= (3, 11))


def test_mutmut_flags_reads_the_makefile() -> None:
    assert mutmut_flags() == ["--group", "dev", "--group", "mutation"]
