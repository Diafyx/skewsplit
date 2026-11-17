"""Check the built distributions of the skewsplit package.

Usage: python scripts/check_dist.py DIST_DIR

Fails unless DIST_DIR holds exactly one wheel and one sdist, the wheel has no
runtime dependencies outside optional extras, and it ships py.typed and the
licence text.
"""

from __future__ import annotations

import sys
import zipfile
from email.parser import Parser
from pathlib import Path


def check(dist: Path) -> list[str]:
    """Return a list of problems with the distributions in ``dist``."""
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return [f"expected one wheel and one sdist, found {len(wheels)} and {len(sdists)}"]

    problems = []
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        metadata_name = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(wheel.read(metadata_name).decode("utf-8"))

    required = [dep for dep in metadata.get_all("Requires-Dist") or [] if "extra ==" not in dep]
    if required:
        problems.append(f"runtime dependencies are not allowed: {required}")
    if "skewsplit/py.typed" not in names:
        problems.append("skewsplit/py.typed is missing from the wheel")
    if not any(n.endswith(".dist-info/licenses/LICENSE") for n in names):
        problems.append("LICENSE is missing from the wheel")
    if metadata.get("License-Expression") != "Apache-2.0":
        problems.append("License-Expression is not Apache-2.0")
    return problems


def main() -> int:
    """Check the directory named on the command line."""
    problems = check(Path(sys.argv[1]))
    for problem in problems:
        print(f"check_dist: {problem}", file=sys.stderr)
    if not problems:
        print("check_dist: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
