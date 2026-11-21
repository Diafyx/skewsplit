from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

import tracking_issue
from tracking_issue import LIMIT, main, run_gh, track

if TYPE_CHECKING:
    from pathlib import Path

TITLE = "Nightly mutation testing of the core is below 90%"
LIST = ["issue", "list", "--state", "open", "--limit", str(LIMIT), "--json", "number,title"]


class FakeGh:
    """Answers the issue list with the given issues and records every call."""

    def __init__(self, issues: list[dict[str, object]]) -> None:
        self.issues = issues
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        return json.dumps(self.issues) if args[:2] == ["issue", "list"] else ""


@pytest.fixture
def body(tmp_path: Path) -> Path:
    path = tmp_path / "body.md"
    path.write_text("The score fell.\n", encoding="utf-8")
    return path


def test_opens_an_issue_when_none_is_open(body: Path) -> None:
    gh = FakeGh([{"number": 7, "title": f"{TITLE} again"}, {"number": 8, "title": "Other"}])
    assert track(TITLE, ["type/test", "area/core"], body, dry_run=False, gh=gh) == (
        "opened a new issue"
    )
    create = ["issue", "create", "--title", TITLE, "--body-file", str(body)]
    assert gh.calls == [LIST, [*create, "--label", "type/test", "--label", "area/core"]]


def test_comments_on_the_open_issue_with_exactly_the_title(body: Path) -> None:
    gh = FakeGh([{"number": 12, "title": TITLE.lower()}, {"number": 9, "title": TITLE}])
    assert track(TITLE, ["type/test"], body, dry_run=False, gh=gh) == "commented on #9"
    assert gh.calls == [LIST, ["issue", "comment", "9", "--body-file", str(body)]]


def test_comments_on_the_oldest_of_two_open_issues(body: Path) -> None:
    gh = FakeGh([{"number": 15, "title": TITLE}, {"number": 11, "title": TITLE}])
    assert track(TITLE, ["type/test"], body, dry_run=False, gh=gh) == "commented on #11"


@pytest.mark.parametrize(
    ("issues", "would"),
    [
        ([], "would have opened a new issue: gh issue create --title "),
        ([{"number": 4, "title": TITLE}], "would have commented on #4: gh issue comment 4 "),
    ],
)
def test_a_dry_run_looks_up_and_writes_nothing(
    issues: list[dict[str, object]], would: str, body: Path
) -> None:
    gh = FakeGh(issues)
    done = track(TITLE, ["type/test"], body, dry_run=True, gh=gh)
    assert done.startswith(f"dry run: {would}")
    assert gh.calls == [LIST]


def test_main_prints_what_it_did(
    body: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh([{"number": 3, "title": TITLE}])
    monkeypatch.setattr(tracking_issue, "run_gh", gh)
    argv = ["--title", TITLE, "--label", "type/test", "--body-file", str(body), "--dry-run"]
    assert main(argv) == 0
    assert capsys.readouterr().out.startswith("dry run: would have commented on #3: ")


def test_main_refuses_an_empty_body(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        main(["--title", TITLE, "--label", "type/test", "--body-file", str(empty)])
    assert raised.value.code == 2
    assert "is empty" in capsys.readouterr().err


def test_run_gh_returns_the_output_and_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        code = 0 if args[1] == "ok" else 1
        return subprocess.CompletedProcess(args, code, stdout="out\n", stderr=" no token \n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_gh(["ok"]) == "out\n"
    with pytest.raises(RuntimeError, match=r"^gh issue list failed: no token$"):
        run_gh(["issue", "list"])
