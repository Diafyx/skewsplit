from __future__ import annotations

import json
import re
import threading
import urllib.error
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import pr_hygiene
from pr_hygiene import HEADINGS, Issue, PullRequest, check, linked_issues

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO = Path(__file__).parents[4]
BODY = "Closes #3\n\n" + "\n\n".join(f"{h}\nN/A" for h in HEADINGS) + "\n"
GOOD = PullRequest(
    title="ci(ci): add pr-hygiene and label sync workflows",
    branch="ci/3-pr-hygiene",
    body=BODY,
    labels=frozenset({"type/ci", "area/ci"}),
    milestone="bootstrap",
    author_is_bot=False,
)
OPEN_3: dict[int, Issue | None] = {
    3: Issue(number=3, is_open=True, is_pull_request=False, milestone="bootstrap")
}


def problems(pr: PullRequest, issues: dict[int, Issue | None] | None = None) -> list[str]:
    return check(pr, OPEN_3 if issues is None else issues)


def test_a_pull_request_that_follows_every_rule_passes() -> None:
    assert problems(GOOD) == []


# Title


@pytest.mark.parametrize(
    "title",
    [
        "feat(core): give dominant values their own predicate",
        "feat(core)!: change the predicate format",
        "fix(postgres): quote mixed-case table names",
        "build(deps): bump ruff from 0.17.5 to 0.17.6",
        "chore(release): release python 0.1.0",
    ],
)
def test_conventional_titles_pass(title: str) -> None:
    assert pr_hygiene.check_title(replace(GOOD, title=title)) == []


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ("Add CI pipeline", "is not `type(scope): summary`"),
        ("ci: add CI pipeline", "is not `type(scope): summary`"),
        ("feature(ci): add CI", "is not `type(scope): summary`"),
        ("ci(ci):add CI", "is not `type(scope): summary`"),
        ("ci(workflows): add CI", "scope `workflows` is not allowed"),
        ("ci(ci): add CI.", "ends with a full stop"),
    ],
)
def test_bad_titles_fail_with_a_fix(title: str, message: str) -> None:
    assert message in " ".join(pr_hygiene.check_title(replace(GOOD, title=title)))


# Labels


@pytest.mark.parametrize(
    ("title", "labels"),
    [
        ("fix(ci): repair the gate", {"type/bug", "area/ci"}),
        ("fix(ci): pin a vulnerable action", {"type/security", "area/ci"}),
        ("revert(ci): undo the gate change", {"type/ci", "area/ci"}),
        ("build(deps): bump ruff", {"type/build", "area/python"}),
    ],
)
def test_matching_labels_pass(title: str, labels: set[str]) -> None:
    pr = replace(GOOD, title=title, labels=frozenset(labels))
    assert pr_hygiene.check_labels(pr) == []


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ({"area/ci"}, "exactly one `type/*` label (found 0)"),
        ({"type/ci", "type/build", "area/ci"}, "exactly one `type/*` label (found 2)"),
        (
            {"type/feature", "area/ci"},
            "`type/feature` does not match title type `ci`. Use `type/ci`",
        ),
        ({"type/ci"}, "at least one `area/*` label"),
        ({"type/ci", "area/python"}, "needs the `area/ci` label"),
    ],
)
def test_wrong_labels_fail_with_a_fix(labels: set[str], message: str) -> None:
    found = pr_hygiene.check_labels(replace(GOOD, labels=frozenset(labels)))
    assert message in " ".join(found)


# Branch


def test_branch_with_the_type_and_linked_issue_passes() -> None:
    assert pr_hygiene.check_branch(GOOD, [3]) == []


@pytest.mark.parametrize(
    ("branch", "message"),
    [
        ("pr-hygiene", "is not `<type>/<issue-number>-<short-slug>`"),
        ("ci/pr-hygiene", "is not `<type>/<issue-number>-<short-slug>`"),
        ("ci/3-PR_hygiene", "is not `<type>/<issue-number>-<short-slug>`"),
        ("feat/3-pr-hygiene", "type `feat` does not match title type `ci`"),
        ("ci/4-pr-hygiene", "issue #4 is not linked"),
    ],
)
def test_bad_branches_fail_with_a_fix(branch: str, message: str) -> None:
    assert message in " ".join(pr_hygiene.check_branch(replace(GOOD, branch=branch), [3]))


# Linked issue and milestone


@pytest.mark.parametrize(
    ("body", "numbers"),
    [
        ("Closes #3", [3]),
        ("fixes #12 and Resolves: #7, closed #12", [7, 12]),
        ("Closes #\n<!-- Closes #9 -->", []),
        ("Relates to #3", []),
    ],
)
def test_linked_issues(body: str, numbers: list[int]) -> None:
    assert linked_issues(body) == numbers


@pytest.mark.parametrize(
    ("pr", "issues", "message"),
    [
        (GOOD, {}, "Link an open issue"),
        (replace(GOOD, milestone=None), OPEN_3, "Set the milestone"),
        (GOOD, {3: None}, "#3 is not an issue"),
        (GOOD, {3: Issue(3, is_open=True, is_pull_request=True, milestone=None)}, "not an issue"),
        (
            GOOD,
            {3: Issue(3, is_open=False, is_pull_request=False, milestone="bootstrap")},
            "closed",
        ),
        (
            GOOD,
            {3: Issue(3, is_open=True, is_pull_request=False, milestone="python v0.1.0")},
            "differs from #3's milestone `python v0.1.0`",
        ),
    ],
)
def test_issue_rules_fail_with_a_fix(
    pr: PullRequest, issues: dict[int, Issue | None], message: str
) -> None:
    assert message in " ".join(pr_hygiene.check_issues(pr, issues))


# Template


def test_missing_template_headings_are_named() -> None:
    body = BODY.replace("### Why is this PR large?\n", "")
    assert pr_hygiene.check_template(replace(GOOD, body=body)) == [
        (
            'Restore the template heading "### Why is this PR large?". '
            'Write "N/A" under it if it does not apply.'
        )
    ]


# Bots


DEPENDABOT = PullRequest(
    title="build(deps): bump ruff from 0.17.5 to 0.17.6",
    branch="dependabot/uv/ruff-0.17.6",
    body="Bumps ruff from 0.17.5 to 0.17.6.",
    labels=frozenset({"dependencies", "type/build", "area/python"}),
    milestone=None,
    author_is_bot=True,
)


def test_dependabot_pull_request_passes_the_exempted_checks() -> None:
    assert check(DEPENDABOT, {}) == []


def test_bots_still_need_a_valid_title_and_labels() -> None:
    pr = replace(DEPENDABOT, title="Bump ruff", labels=frozenset({"dependencies"}))
    assert len(check(pr, {})) == 3


def test_a_human_cannot_borrow_a_bot_branch_name() -> None:
    pr = replace(DEPENDABOT, author_is_bot=False)
    assert "Link an open issue" in " ".join(check(pr, {}))


# Event payload, API and main


def event(pr: PullRequest) -> dict[str, Any]:
    return {
        "pull_request": {
            "title": pr.title,
            "head": {"ref": pr.branch},
            "body": pr.body or None,
            "labels": [{"name": name} for name in sorted(pr.labels)],
            "milestone": {"title": pr.milestone} if pr.milestone else None,
            "user": {"login": "someone", "type": "Bot" if pr.author_is_bot else "User"},
        }
    }


@pytest.mark.parametrize("pr", [GOOD, DEPENDABOT, replace(GOOD, body="", milestone=None)])
def test_event_payload_round_trips(pr: PullRequest) -> None:
    assert pr_hygiene.pull_request_from_event(event(pr)) == pr


ISSUES = {
    "/repos/o/r/issues/3": {"state": "open", "milestone": {"title": "bootstrap"}},
    "/repos/o/r/issues/5": {"state": "closed", "milestone": None},
    "/repos/o/r/issues/6": {"state": "open", "milestone": None, "pull_request": {}},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/repos/o/r/issues/500":
            self.send_response(500)
            self.end_headers()
            return
        data = ISSUES.get(self.path)
        self.send_response(200 if data else 404)
        self.end_headers()
        self.wfile.write(json.dumps(data or {"message": "Not Found"}).encode())

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GITHUB_API_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    yield
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.usefixtures("api")
def test_fetch_issue_reads_state_milestone_and_kind() -> None:
    fetch = pr_hygiene.fetch_issue
    assert fetch("o/r", 3, "t") == Issue(
        3, is_open=True, is_pull_request=False, milestone="bootstrap"
    )
    assert fetch("o/r", 5, "t") == Issue(5, is_open=False, is_pull_request=False, milestone=None)
    assert fetch("o/r", 6, "t") == Issue(6, is_open=True, is_pull_request=True, milestone=None)
    assert fetch("o/r", 404, "t") is None


@pytest.mark.usefixtures("api")
def test_fetch_issue_raises_on_server_errors() -> None:
    with pytest.raises(urllib.error.HTTPError):
        pr_hygiene.fetch_issue("o/r", 500, "t")


def serve_pull_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pr: PullRequest) -> Path:
    """Serve ``pr`` as pull request #7 and write a stale event that names it."""
    monkeypatch.setitem(ISSUES, "/repos/o/r/pulls/7", event(pr)["pull_request"])
    stale = replace(GOOD, labels=frozenset(), milestone=None)  # as the "opened" event saw it
    payload = event(stale)
    payload["pull_request"]["number"] = 7
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.usefixtures("api")
def test_main_checks_the_current_pull_request_not_the_event_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = serve_pull_request(monkeypatch, tmp_path, GOOD)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert pr_hygiene.main([str(path)]) == 0
    assert "follows every rule" in summary.read_text(encoding="utf-8")
    assert "follows every rule" in capsys.readouterr().out


@pytest.mark.usefixtures("api")
def test_main_fails_and_explains_each_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = serve_pull_request(monkeypatch, tmp_path, replace(GOOD, body="Closes #5"))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert pr_hygiene.main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("### :x: pr-hygiene\n")
    assert "- Branch issue #3 is not linked" in err
    assert "- #5 is closed" in err
    assert err.count("Restore the template heading") == len(HEADINGS)


@pytest.mark.usefixtures("api")
def test_a_missing_pull_request_is_an_error() -> None:
    with pytest.raises(LookupError, match="pull request #404 not found in o/r"):
        pr_hygiene.fetch_pull_request("o/r", 404, "t")


# PULL_REQUEST_TEMPLATE.md


def test_the_template_has_exactly_the_headings_pr_hygiene_checks() -> None:
    template = (REPO / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    headings = tuple(line for line in template.splitlines() if line.startswith("### "))
    assert headings == HEADINGS


def test_the_unfilled_template_fails_only_on_the_issue_link() -> None:
    template = (REPO / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    pr = replace(GOOD, body=template)
    assert linked_issues(template) == []
    assert pr_hygiene.check_template(pr) == []


# .github/labels.yml


def labels_file() -> list[dict[str, str]]:
    text = (REPO / ".github" / "labels.yml").read_text(encoding="utf-8")
    pattern = r'- name: "([^"]+)"\n  color: "([^"]+)"\n  description: "([^"]+)"'
    return [{"name": n, "color": c, "description": d} for n, c, d in re.findall(pattern, text)]


def test_labels_file_entries_are_complete_and_unique() -> None:
    labels = labels_file()
    text = (REPO / ".github" / "labels.yml").read_text(encoding="utf-8")
    assert len(labels) == text.count("- name:")
    names = [label["name"] for label in labels]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[0-9a-f]{6}", label["color"]) for label in labels)


def test_labels_file_has_every_label_the_tooling_uses() -> None:
    names = {label["name"] for label in labels_file()}
    used = {f"area/{area}" for area in pr_hygiene.AREAS}
    used |= {label for labels in pr_hygiene.TYPE_LABELS.values() for label in labels}
    used |= {"size/S", "size/M", "size/L", "size/XL", "size/XXL", "ci:full", "dependencies"}
    used |= {"autorelease: pending", "autorelease: tagged", "breaking-change"}
    assert used <= names
