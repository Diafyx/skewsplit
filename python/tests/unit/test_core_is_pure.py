"""The core may import only the standard library and itself (ADR 0001)."""

from __future__ import annotations

import ast
import importlib.util
import sys
import sysconfig
from pathlib import Path

import pytest

import skewsplit.core

CORE = Path(skewsplit.core.__file__).parent
STDLIB_DIRS = {Path(sysconfig.get_paths()[key]).resolve() for key in ("stdlib", "platstdlib")}


def is_stdlib(name: str) -> bool:
    if name in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        return False
    if spec.origin == "frozen":
        return True
    origin = Path(spec.origin).resolve()
    return "site-packages" not in origin.parts and any(d in origin.parents for d in STDLIB_DIRS)


def foreign_imports(source: str, module: str) -> list[str]:
    """Return the imports in ``source`` that leave the standard library and the core."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                package = module.rsplit(".", node.level)[0]
                base = f"{package}.{base}" if base else package
            names = [base]
        else:
            continue
        for name in names:
            inside_core = name == "skewsplit.core" or name.startswith("skewsplit.core.")
            if not inside_core and not is_stdlib(name.split(".")[0]):
                found.append(name)
    return found


CORE_MODULES = {
    ".".join(("skewsplit", *path.relative_to(CORE.parent).with_suffix("").parts)): path
    for path in sorted(CORE.rglob("*.py"))
}


@pytest.mark.parametrize("module", sorted(CORE_MODULES))
def test_core_module_imports_only_stdlib_and_core(module: str) -> None:
    source = CORE_MODULES[module].read_text(encoding="utf-8")
    assert foreign_imports(source, module) == []


@pytest.mark.parametrize("name", ["json", "sys", "itertools", "decimal"])
def test_standard_library_modules_are_recognised(name: str) -> None:
    assert is_stdlib(name)


@pytest.mark.parametrize("name", ["pytest", "skewsplit", "no_such_module_xyz"])
def test_other_modules_are_not_standard_library(name: str) -> None:
    assert not is_stdlib(name)


def test_checker_flags_imports_that_leave_the_core() -> None:
    source = (
        "import json\n"
        "import pytest\n"
        "from . import codecs\n"
        "from .. import cli\n"
        "from skewsplit.core.plan import x\n"
        "from skewsplit.postgres import y\n"
    )
    assert foreign_imports(source, "skewsplit.core.planner") == [
        "pytest",
        "skewsplit",
        "skewsplit.postgres",
    ]
