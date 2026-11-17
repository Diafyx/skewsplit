from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import pytest

import check_links
from check_links import anchors, check_file, links

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

DOC = """# Title

See [the ADR](adr/0001.md#context), [missing](nope.md) and <https://example.invalid/x>.
![logo](img/logo.png "Logo") and [mail](mailto:someone@example.invalid).
Inline `[not a link](ignored.md)` code.

```markdown
[also ignored](ignored.md)
```

## Same name
## Same name
"""


def test_links_skip_code_spans_and_fenced_blocks() -> None:
    assert links(DOC) == [
        "adr/0001.md#context",
        "nope.md",
        "https://example.invalid/x",
        "img/logo.png",
        "mailto:someone@example.invalid",
    ]


def test_links_include_both_targets_of_a_badge() -> None:
    text = "[![CI](https://x.invalid/badge.svg)](https://x.invalid/workflow) [a](b.md)"
    assert links(text) == ["https://x.invalid/badge.svg", "https://x.invalid/workflow", "b.md"]


def test_anchors_follow_github_slugs_with_duplicate_suffixes() -> None:
    text = "# Hello, World!\n## Why is this PR large?\n## Same name\n## Same name\n# Closed #\n"
    assert anchors(text) == {
        "hello-world",
        "why-is-this-pr-large",
        "same-name",
        "same-name-1",
        "closed",
    }


def test_headings_inside_fences_are_not_anchors() -> None:
    assert anchors("~~~\n# not a heading\n~~~\n# real\n") == {"real"}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "adr").mkdir()
    (tmp_path / "adr" / "0001.md").write_text("# ADR\n## Context\n", encoding="utf-8")
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "logo.png").write_bytes(b"")
    return tmp_path


def test_local_links_must_exist_and_anchors_must_match(tree: Path) -> None:
    doc = tree / "README.md"
    doc.write_text(
        DOC + "[bad anchor](adr/0001.md#nope) [self](#same-name-1) [dir](adr)\n",
        encoding="utf-8",
    )
    problems = check_file(doc, external=False)
    assert len(problems) == 2
    assert "nope.md: " in problems[0]
    assert "no heading for #nope" in problems[1]


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        codes = {"/ok": 200, "/gone": 404, "/nohead": 405, "/busy": 429, "/broken": 500}
        self.send_response(codes.get(self.path, 404))
        self.end_headers()

    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/nohead" else 404)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("/ok", None),
        ("/nohead", None),
        ("/busy", None),
        ("/gone", "HTTP 404"),
        ("/broken", "HTTP 500"),
    ],
)
def test_external_links(server: str, path: str, problem: str | None) -> None:
    result = check_links.check_external(server + path)
    assert result is None if problem is None else problem in (result or "")


def test_unreachable_hosts_are_reported() -> None:
    assert "nothing.invalid" in (check_links.check_external("http://nothing.invalid/") or "")


def test_main_checks_external_links_only_when_asked(
    tmp_path: Path, server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(f"[gone]({server}/gone)\n", encoding="utf-8")
    assert check_links.main([str(doc)]) == 0
    assert check_links.main(["--external", str(doc)]) == 1
    captured = capsys.readouterr()
    assert "HTTP 404" in captured.err
    assert "1 files, 1 problems" in captured.out
