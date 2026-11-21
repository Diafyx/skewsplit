from __future__ import annotations

from typing import TYPE_CHECKING

from extract_examples import examples, main, sources

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

MODULE = '''"""A module.

>>> import json
>>> data = json.loads(
...     "[1]"
... )
"""


def documented() -> None:
    """A function.

    >>> documented()
    """


def undocumented() -> None:
    """No examples here."""


class Box:
    """A class without examples."""

    async def method(self) -> None:
        """A method.

        >>> box = Box()
        """
'''

MARKDOWN = """# Title

```python
>>> answer = 6 * 7
>>> answer
42
```

Text, then more of the same session:

>>> answer += 1
"""


def test_examples_keep_the_source_and_drop_the_output() -> None:
    assert examples(">>> x = 1\n>>> x\n1\n... not a continuation\n") == "x = 1\nx\n"
    assert examples("no examples") == ""


def test_each_docstring_with_examples_is_one_session(tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_text(MODULE, encoding="utf-8")
    assert sources(path) == {
        "0": 'import json\ndata = json.loads(\n    "[1]"\n)\n',
        "10": "documented()\n",
        "24": "box = Box()\n",
    }


def test_a_markdown_file_is_one_session(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    path.write_text(MARKDOWN, encoding="utf-8")
    assert sources(path) == {"": "answer = 6 * 7\nanswer\nanswer += 1\n"}


def test_main_writes_one_module_per_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(MODULE, encoding="utf-8")
    (tmp_path / "src" / "pkg" / "empty.py").write_text('"""None."""\n', encoding="utf-8")
    (tmp_path / "README.md").write_text(MARKDOWN, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "example_stale.py").write_text("stale = True\n", encoding="utf-8")
    (out / "keep.txt").write_text("not ours\n", encoding="utf-8")

    assert main(["out", "src", "README.md"]) == 0
    assert sorted(path.name for path in out.iterdir()) == [
        "example_README_md.py",
        "example_src_pkg_mod_py_0.py",
        "example_src_pkg_mod_py_10.py",
        "example_src_pkg_mod_py_24.py",
        "keep.txt",
    ]
    assert (out / "example_src_pkg_mod_py_10.py").read_text(encoding="utf-8") == "documented()\n"
    assert capsys.readouterr().out == "wrote 4 example modules to out\n"


def test_main_fails_without_examples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "empty.py").write_text('"""None."""\n', encoding="utf-8")
    assert main(["out", "empty.py"]) == 1
    assert capsys.readouterr().err == "no >>> examples found\n"


def test_main_needs_a_folder_and_a_path(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["out"]) == 2
    assert capsys.readouterr().err.startswith("Usage: python scripts/extract_examples.py")
