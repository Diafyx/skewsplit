"""Write the >>> examples of docstrings and Markdown files as modules, for mypy.

Usage: python scripts/extract_examples.py OUT_DIR PATH...

Each PATH is a Python file, a folder of them, or a Markdown file. doctest runs
the examples of one docstring, or of one Markdown file, as one session, so
each becomes one module: OUT_DIR/example_<path>_<where>.py. The modules a
previous run wrote there are removed first. Fails if no PATH has an example.
"""

from __future__ import annotations

import ast
import doctest
import re
import sys
from pathlib import Path

_DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def examples(text: str) -> str:
    """Return the source of the examples in ``text``, or an empty string."""
    return "".join(example.source for example in doctest.DocTestParser().get_examples(text))


def sources(path: Path) -> dict[str, str]:
    """Return the examples of each docstring or Markdown file in ``path`` that has any.

    The keys say where the examples are: the line of the docstring's
    definition, 0 for a module, or empty for a Markdown file.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".md":
        found = {"": examples(text)}
    else:
        found = {}
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(node, _DOCUMENTED):
                docstring = ast.get_docstring(node)
                if docstring:
                    found[str(getattr(node, "lineno", 0))] = examples(docstring)
    return {where: source for where, source in found.items() if source}


def files(paths: list[Path]) -> list[Path]:
    """Return the files of ``paths``, with each folder replaced by its Python files."""
    out: list[Path] = []
    for path in paths:
        out.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return out


def write(out_dir: Path, paths: list[Path]) -> int:
    """Write the modules and return how many there are."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("example_*.py"):
        old.unlink()
    count = 0
    for path in files(paths):
        for where, source in sources(path).items():
            name = re.sub(r"\W", "_", "_".join(filter(None, [path.as_posix(), where])))
            (out_dir / f"example_{name}.py").write_text(source, encoding="utf-8")
            count += 1
    return count


def main(argv: list[str]) -> int:
    """Run the command line and return its exit code."""
    if len(argv) < 2:  # noqa: PLR2004 -- OUT_DIR and at least one PATH
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    count = write(Path(argv[0]), [Path(arg) for arg in argv[1:]])
    if count == 0:
        print("no >>> examples found", file=sys.stderr)
        return 1
    print(f"wrote {count} example modules to {argv[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
