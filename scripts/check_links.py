"""Check the links in Markdown files.

Usage: python scripts/check_links.py [--external] FILE...

Relative links must point to an existing file, and ``#anchors`` must match a
heading in the target Markdown file. With ``--external``, http(s) links are
requested as well. Links inside code spans and fenced code blocks are ignored.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

TIMEOUT = 15
HTTP_ERROR = 400
# Refusals that some servers give to HEAD requests or unknown clients.
RETRY_WITH_GET = frozenset({403, 404, 405, 501})
# Rate limited: the page exists.
TOO_MANY_REQUESTS = 429
USER_AGENT = "skewsplit-link-check"
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+).+?\1")
# Every "](target)" is a link target, which also catches the outer link of a
# badge such as [![CI](badge.svg)](workflow), plus <https://...> autolinks.
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)|<(https?://[^>\s]+)>")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")


def prose_lines(text: str) -> list[str]:
    """Return the lines outside fenced code blocks, with code spans removed."""
    lines, fence = [], ""
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match and (not fence or match.group(1).startswith(fence)):
            fence = "" if fence else match.group(1)[0] * 3
            continue
        if not fence:
            lines.append(_CODE_SPAN.sub("", line))
    return lines


def links(text: str) -> list[str]:
    """Return every link target in the Markdown text."""
    found = []
    for line in prose_lines(text):
        found += [m.group(1) or m.group(2) for m in _LINK.finditer(line)]
    return found


def anchors(text: str) -> set[str]:
    """Return the GitHub-style anchors of the headings in the Markdown text."""
    seen: dict[str, int] = {}
    for line in prose_lines(text):
        match = _HEADING.match(line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match.group(1).lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        if count:
            seen[f"{slug}-{count}"] = 1
    return set(seen)


def check_local(source: Path, target: str) -> str | None:
    """Return a problem with a relative link, or None if it is fine."""
    parts = urlsplit(target)
    path = (source.parent / unquote(parts.path)) if parts.path else source
    if not path.exists():
        return f"{target}: {path} does not exist"
    if (
        parts.fragment
        and path.suffix == ".md"
        and parts.fragment not in anchors(path.read_text(encoding="utf-8"))
    ):
        return f"{target}: no heading for #{parts.fragment} in {path}"
    return None


def _status(url: str, method: str) -> int | str:
    """Return the HTTP status of a request, or the error if there was no answer."""
    request = urllib.request.Request(  # noqa: S310 -- callers pass http(s) URLs only
        url, method=method, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 -- as above
            return int(response.status)
    except urllib.error.HTTPError as error:
        error.close()
        return error.code
    except (urllib.error.URLError, TimeoutError) as error:
        return str(error)


def check_external(url: str) -> str | None:
    """Return a problem with an http(s) link, or None if it answers."""
    status = _status(url, "HEAD")
    if status in RETRY_WITH_GET:
        status = _status(url, "GET")
    if isinstance(status, str):
        return f"{url}: {status}"
    if status < HTTP_ERROR or status == TOO_MANY_REQUESTS:
        return None
    return f"{url}: HTTP {status}"


def check_file(path: Path, *, external: bool) -> list[str]:
    """Return the problems with the links in one Markdown file."""
    problems = []
    for target in links(path.read_text(encoding="utf-8")):
        scheme = urlsplit(target).scheme
        if scheme in {"http", "https"}:
            problem = check_external(target) if external else None
        elif scheme:
            problem = None  # mailto: and similar are not checked
        else:
            problem = check_local(path, target)
        if problem:
            problems.append(f"{path}: {problem}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Check the files named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--external", action="store_true", help="also request http(s) links")
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    problems = [p for f in args.files for p in check_file(f, external=args.external)]
    for problem in problems:
        print(f"check-links: {problem}", file=sys.stderr)
    print(f"check-links: {len(args.files)} files, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
