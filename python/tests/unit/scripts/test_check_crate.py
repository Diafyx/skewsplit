from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING

import pytest

from check_crate import check, dependencies, main

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

MANIFEST = """\
[package]
name = "skewsplit"
version = "0.1.0"
license = "Apache-2.0"

[lib]
name = "skewsplit"
path = "src/lib.rs"

[dependencies]

[dev-dependencies.proptest]
version = "1.12"
"""
FILES = {
    ".cargo_vcs_info.json": "{}",
    "Cargo.lock": "version = 4\n",
    "Cargo.toml": MANIFEST,
    "Cargo.toml.orig": "[package]\n",
    "LICENSE": "                                 Apache License\n",
    "README.md": "# skewsplit\n",
    "src/lib.rs": "",
    "src/main.rs": "",
    "src/planner.rs": "",
}


def crate(
    tmp_path: Path, files: Mapping[str, str | None], prefix: str = "skewsplit-0.1.0/"
) -> Path:
    """Write a .crate file; a None value is a symbolic link to LICENSE."""
    path = tmp_path / "skewsplit-0.1.0.crate"
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            member = tarfile.TarInfo(prefix + name)
            if text is None:
                member.type = tarfile.SYMTYPE
                member.linkname = "../LICENSE"
                archive.addfile(member)
            else:
                data = text.encode("utf-8")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
    return path


def test_the_package_cargo_writes_passes(tmp_path: Path) -> None:
    files, problems = check(crate(tmp_path, {**FILES}))
    assert problems == []
    assert files == sorted(FILES)


def test_a_changelog_and_nested_sources_are_allowed(tmp_path: Path) -> None:
    path = crate(tmp_path, {**FILES, "CHANGELOG.md": "", "src/plan/text.rs": ""})
    assert check(path)[1] == []


@pytest.mark.parametrize(
    "name", ["tests/cli.rs", "spec/fixtures/a.json", "src/notes.txt", "Makefile"]
)
def test_a_file_include_does_not_ask_for_fails(tmp_path: Path, name: str) -> None:
    assert check(crate(tmp_path, {**FILES, name: ""}))[1] == [f"{name} is not asked for by include"]


@pytest.mark.parametrize("name", ["LICENSE", "README.md", "src/main.rs", "Cargo.lock"])
def test_a_missing_file_fails(tmp_path: Path, name: str) -> None:
    files = {key: text for key, text in FILES.items() if key != name}
    assert check(crate(tmp_path, files))[1] == [f"{name} is missing"]


def test_a_symbolic_link_fails(tmp_path: Path) -> None:
    files: dict[str, str | None] = {**FILES, "LICENSE": None}
    assert check(crate(tmp_path, files))[1] == ["LICENSE is not a regular file"]


def test_another_licence_fails(tmp_path: Path) -> None:
    path = crate(tmp_path, {**FILES, "LICENSE": "MIT License\n"})
    assert check(path)[1] == ["LICENSE is not the Apache License"]


def test_a_manifest_without_the_licence_fails(tmp_path: Path) -> None:
    manifest = MANIFEST.replace('license = "Apache-2.0"\n', 'license = "MIT"\n')
    path = crate(tmp_path, {**FILES, "Cargo.toml": manifest})
    assert check(path)[1] == ['Cargo.toml has no license = "Apache-2.0"']


def test_files_outside_the_crate_folder_fail(tmp_path: Path) -> None:
    files, problems = check(crate(tmp_path, {**FILES}, prefix="other-0.1.0/"))
    assert files == []
    assert problems[0].startswith("files outside skewsplit-0.1.0/: ")


def test_an_empty_dependency_table_is_no_dependency() -> None:
    assert dependencies(MANIFEST) == []
    assert dependencies("[dependencies]\n") == []
    assert dependencies("[package]\n") == []


@pytest.mark.parametrize(
    "table",
    [
        '[dependencies]\nserde = "1"\n',
        '[dependencies.serde]\nversion = "1"\n',
        '[build-dependencies.cc]\nversion = "1"\n',
        "[target.'cfg(unix)'.dependencies.libc]\nversion = \"0.2\"\n",
    ],
)
def test_a_dependency_fails(tmp_path: Path, table: str) -> None:
    path = crate(tmp_path, {**FILES, "Cargo.toml": MANIFEST + "\n" + table})
    problems = check(path)[1]
    assert len(problems) == 1
    assert problems[0] == f"Cargo.toml has dependencies: {table.splitlines()[0]}"


def test_dev_dependencies_are_not_dependencies() -> None:
    assert dependencies('[dev-dependencies]\nproptest = "1"\n') == []
    assert dependencies("[target.'cfg(unix)'.dev-dependencies.libc]\n") == []


def test_main_lists_the_files_and_reports_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["check_crate.py", str(crate(tmp_path, {**FILES}))])
    assert main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "check_crate: ok"
    assert "check_crate: src/lib.rs" in out


def test_main_fails_with_the_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = crate(tmp_path, {**FILES, "tests/cli.rs": ""})
    monkeypatch.setattr("sys.argv", ["check_crate.py", str(path)])
    assert main() == 1
    captured = capsys.readouterr()
    assert "check_crate: ok" not in captured.out
    assert captured.err == "check_crate: tests/cli.rs is not asked for by include\n"
