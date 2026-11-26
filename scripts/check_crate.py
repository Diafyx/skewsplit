"""Check the packaged crate, the file that `cargo publish` uploads to crates.io.

Usage: python scripts/check_crate.py CRATE

CRATE is the `.crate` file that `cargo package` or `cargo publish --dry-run`
writes to rust/target/package. The check fails unless it holds the library,
the binary, the README and the licence text, and nothing that `include` in
rust/Cargo.toml does not ask for. The manifest cargo publishes must keep the
licence and have no dependencies.

The file list is printed, so the release log shows what was published.
"""

from __future__ import annotations

import re
import sys
import tarfile
from pathlib import Path

# The files cargo adds to every package, and the ones `include` asks for.
REQUIRED = frozenset(
    {"Cargo.toml", "Cargo.lock", "LICENSE", "README.md", "src/lib.rs", "src/main.rs"}
)
OPTIONAL = frozenset({"Cargo.toml.orig", ".cargo_vcs_info.json", "CHANGELOG.md"})
_SOURCE = re.compile(r"src/(?:[a-z0-9_]+/)*[a-z0-9_]+\.rs")
# A dependency table, in either form cargo writes: [dependencies.name] or
# [target.'cfg(...)'.dependencies]. Dev-dependencies are only for the tests.
_DEPENDENCY_TABLE = re.compile(
    r"^\[(?:target\..+\.)?(?:build-)?dependencies(?:\.[^\]]+)?\]$", re.MULTILINE
)


def allowed(name: str) -> bool:
    """Return whether include in rust/Cargo.toml, or cargo itself, adds this file."""
    return name in REQUIRED or name in OPTIONAL or _SOURCE.fullmatch(name) is not None


def dependencies(manifest: str) -> list[str]:
    """Return the dependency tables of a manifest that are not empty."""
    found = []
    for match in _DEPENDENCY_TABLE.finditer(manifest):
        rest = manifest[match.end() :].lstrip("\n")
        if match.group() != "[dependencies]" or (rest and not rest.startswith("[")):
            found.append(match.group())
    return found


def read(archive: tarfile.TarFile, member: tarfile.TarInfo | None) -> str | None:
    """Return the text of a regular file in the archive, or None."""
    stream = archive.extractfile(member) if member is not None and member.isfile() else None
    return None if stream is None else stream.read().decode("utf-8")


def check(crate: Path) -> tuple[list[str], list[str]]:
    """Return the files in ``crate`` and a list of problems with it."""
    prefix = crate.name.removesuffix(".crate") + "/"
    with tarfile.open(crate, "r:gz") as archive:
        members = archive.getmembers()
        outside = [m.name for m in members if not m.name.startswith(prefix)]
        if outside:
            return [], [f"files outside {prefix}: {outside}"]
        files = {m.name[len(prefix) :]: m for m in members}
        manifest = read(archive, files.get("Cargo.toml"))
        licence = read(archive, files.get("LICENSE"))
    problems = [f"{name} is not a regular file" for name, m in files.items() if not m.isfile()]
    problems += [f"{name} is missing" for name in sorted(REQUIRED - files.keys())]
    problems += [f"{name} is not asked for by include" for name in files if not allowed(name)]
    if licence is not None and "Apache License" not in licence:
        problems.append("LICENSE is not the Apache License")
    if manifest is not None:
        if not re.search(r'^license = "Apache-2.0"$', manifest, re.MULTILINE):
            problems.append('Cargo.toml has no license = "Apache-2.0"')
        problems += [f"Cargo.toml has dependencies: {table}" for table in dependencies(manifest)]
    return sorted(files), problems


def main() -> int:
    """Check the crate named on the command line."""
    files, problems = check(Path(sys.argv[1]))
    for name in files:
        print(f"check_crate: {name}")
    for problem in problems:
        print(f"check_crate: {problem}", file=sys.stderr)
    if not problems:
        print("check_crate: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
