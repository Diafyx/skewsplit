"""Check a pull request against the repository's pull request rules.

Usage: python scripts/pr_hygiene.py [EVENT_FILE]

Reads the pull request named in the pull_request event (default:
``$GITHUB_EVENT_PATH``) and its linked issues through the GitHub API, and
lists every broken rule with what to do about it. The list goes to stderr and
to ``$GITHUB_STEP_SUMMARY``. Exits 1 if any rule is broken.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TYPES = ("feat", "fix", "perf", "refactor", "test", "docs", "ci", "build", "chore", "revert")
AREAS = (
    "core",
    "python",
    "rust",
    "postgres",
    "spark",
    "cli",
    "spec",
    "bench",
    "ci",
    "docs",
    "release",
)
SCOPES = (*AREAS, "deps")
# The type/* labels each title type accepts. A revert takes the label of what it reverts.
TYPE_LABELS: dict[str, frozenset[str]] = {
    "feat": frozenset({"type/feature"}),
    "fix": frozenset({"type/bug", "type/security"}),
    "perf": frozenset({"type/perf"}),
    "refactor": frozenset({"type/refactor"}),
    "test": frozenset({"type/test"}),
    "docs": frozenset({"type/docs"}),
    "ci": frozenset({"type/ci"}),
    "build": frozenset({"type/build"}),
    "chore": frozenset({"type/chore"}),
}
HEADINGS = (
    "### What changes were proposed in this pull request?",
    "### Why are the changes needed?",
    "### Does this PR introduce _any_ user-facing change?",
    "### How was this patch tested?",
    "### Does this PR potentially affect any of the following?",
    "### Why is this PR large?",
    "### Checklist",
)
BOT_BRANCH_PREFIXES = ("dependabot/", "release-please--")

_TYPE = "|".join(TYPES)
_TITLE = re.compile(rf"(?P<type>{_TYPE})\((?P<scope>[a-z0-9-]+)\)!?: (?P<summary>\S.*)")
_BRANCH = re.compile(rf"(?P<type>{_TYPE})/(?P<issue>[1-9][0-9]*)-[a-z0-9]+(?:-[a-z0-9]+)*")
_LINK = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#([1-9][0-9]*)\b", re.IGNORECASE
)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class PullRequest:
    """The parts of a pull request that the rules look at."""

    title: str
    branch: str
    body: str
    labels: frozenset[str]
    milestone: str | None
    author_is_bot: bool


@dataclass(frozen=True)
class Issue:
    """A linked issue as the GitHub API reports it."""

    number: int
    is_open: bool
    is_pull_request: bool
    milestone: str | None


def is_exempt_bot(pr: PullRequest) -> bool:
    """Return whether this is a Dependabot or release-please pull request.

    Both conditions must hold, so naming a branch like a bot's skips nothing.
    """
    return pr.author_is_bot and pr.branch.startswith(BOT_BRANCH_PREFIXES)


def linked_issues(body: str) -> list[int]:
    """Return the issue numbers after Closes, Fixes or Resolves, outside comments."""
    numbers = (int(n) for n in _LINK.findall(_COMMENT.sub("", body)))
    return sorted(set(numbers))


def check_title(pr: PullRequest) -> list[str]:
    """Check the Conventional Commits title and its scope."""
    match = _TITLE.fullmatch(pr.title)
    if not match:
        return [
            (
                f"Title `{pr.title}` is not `type(scope): summary`. Types: {', '.join(TYPES)}. "
                "Add `!` before the colon for a breaking change."
            )
        ]
    problems = []
    if match.group("scope") not in SCOPES:
        problems.append(
            f"Title scope `{match.group('scope')}` is not allowed. Use an area "
            f"({', '.join(AREAS)}) or `deps`."
        )
    if match.group("summary").endswith("."):
        problems.append("Title summary ends with a full stop. Remove it.")
    return problems


def check_labels(pr: PullRequest) -> list[str]:
    """Check the type/* and area/* labels against the title."""
    problems = []
    types = sorted(label for label in pr.labels if label.startswith("type/"))
    match = _TITLE.fullmatch(pr.title)
    if len(types) != 1:
        problems.append(f"Set exactly one `type/*` label (found {len(types)}).")
    elif match and match.group("type") in TYPE_LABELS:
        expected = TYPE_LABELS[match.group("type")]
        if types[0] not in expected:
            wanted = " or ".join(f"`{label}`" for label in sorted(expected))
            problems.append(
                f"Label `{types[0]}` does not match title type `{match.group('type')}`. "
                f"Use {wanted}."
            )
    if not any(label.startswith("area/") for label in pr.labels):
        problems.append("Add at least one `area/*` label.")
    if match and match.group("scope") in AREAS and f"area/{match.group('scope')}" not in pr.labels:
        problems.append(
            f"Title scope `{match.group('scope')}` needs the `area/{match.group('scope')}` label."
        )
    return problems


def check_branch(pr: PullRequest, issues: list[int]) -> list[str]:
    """Check the branch name and that its issue number is linked."""
    match = _BRANCH.fullmatch(pr.branch)
    if not match:
        return [
            (
                f"Branch `{pr.branch}` is not `<type>/<issue-number>-<short-slug>`, "
                "for example `feat/12-pg-stats-strategy`."
            )
        ]
    problems = []
    title = _TITLE.fullmatch(pr.title)
    if title and title.group("type") != match.group("type"):
        problems.append(
            f"Branch type `{match.group('type')}` does not match title type "
            f"`{title.group('type')}`."
        )
    if issues and int(match.group("issue")) not in issues:
        problems.append(
            f"Branch issue #{match.group('issue')} is not linked in the description. "
            f"Add `Closes #{match.group('issue')}`."
        )
    return problems


def check_issues(pr: PullRequest, issues: dict[int, Issue | None]) -> list[str]:
    """Check that the linked issues exist, are open and share the milestone."""
    if not issues:
        return ["Link an open issue in the description, for example `Closes #12`."]
    problems = []
    if pr.milestone is None:
        problems.append("Set the milestone of the linked issue on this pull request.")
    for number, issue in sorted(issues.items()):
        if issue is None or issue.is_pull_request:
            problems.append(f"#{number} is not an issue in this repository.")
        elif not issue.is_open:
            problems.append(f"#{number} is closed. Link an open issue, or reopen it.")
        elif pr.milestone is not None and issue.milestone != pr.milestone:
            problems.append(
                f"Milestone `{pr.milestone}` differs from #{number}'s milestone "
                f"`{issue.milestone}`. Use the issue's milestone."
            )
    return problems


def check_template(pr: PullRequest) -> list[str]:
    """Check that every heading of the pull request template is present."""
    lines = {line.strip() for line in pr.body.splitlines()}
    return [
        f'Restore the template heading "{heading}". Write "N/A" under it if it does not apply.'
        for heading in HEADINGS
        if heading not in lines
    ]


def check(pr: PullRequest, issues: dict[int, Issue | None]) -> list[str]:
    """Return every broken rule, each with what to do about it."""
    problems = check_title(pr) + check_labels(pr)
    if is_exempt_bot(pr):
        return problems
    problems += check_branch(pr, sorted(issues))
    problems += check_issues(pr, issues)
    problems += check_template(pr)
    return problems


def pull_request_from_event(event: dict[str, Any]) -> PullRequest:
    """Build a PullRequest from a pull_request event payload."""
    data = event["pull_request"]
    milestone = data.get("milestone") or {}
    return PullRequest(
        title=data["title"],
        branch=data["head"]["ref"],
        body=data.get("body") or "",
        labels=frozenset(label["name"] for label in data.get("labels", [])),
        milestone=milestone.get("title"),
        author_is_bot=data["user"]["type"] == "Bot",
    )


def _get(path: str, token: str) -> dict[str, Any] | None:
    """GET a GitHub REST API path; None if it does not exist."""
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(  # noqa: S310 -- the API URL comes from the runner
        f"{api}/{path}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- as above
            data: dict[str, Any] = json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 404:  # noqa: PLR2004 -- HTTP Not Found
            return None
        raise
    return data


def fetch_issue(repo: str, number: int, token: str) -> Issue | None:
    """Look up an issue through the GitHub API; None if it does not exist."""
    data = _get(f"repos/{repo}/issues/{number}", token)
    if data is None:
        return None
    milestone = data.get("milestone") or {}
    return Issue(
        number=number,
        is_open=data["state"] == "open",
        is_pull_request="pull_request" in data,
        milestone=milestone.get("title"),
    )


def fetch_pull_request(repo: str, number: int, token: str) -> PullRequest:
    """Read the pull request as it is now, not as the triggering event saw it.

    Creating a pull request with labels and a milestone fires several events
    within a second or two. Reading the current state lets every run see the
    finished pull request, so no run has to be cancelled or fail early.
    """
    data = _get(f"repos/{repo}/pulls/{number}", token)
    if data is None:
        msg = f"pull request #{number} not found in {repo}"
        raise LookupError(msg)
    return pull_request_from_event({"pull_request": data})


def render(problems: list[str]) -> str:
    """Format the result for the job summary."""
    if not problems:
        return "### :white_check_mark: pr-hygiene\n\nThis pull request follows every rule.\n"
    lines = ["### :x: pr-hygiene", "", "Fix these, then the check runs again:", ""]
    lines += [f"- {problem}" for problem in problems]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Check the pull request named in the event file."""
    args = sys.argv[1:] if argv is None else argv
    event_path = Path(args[0] if args else os.environ["GITHUB_EVENT_PATH"])
    event = json.loads(event_path.read_text(encoding="utf-8"))
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    pr = fetch_pull_request(repo, int(event["pull_request"]["number"]), token)
    numbers = [] if is_exempt_bot(pr) else linked_issues(pr.body)
    problems = check(pr, {n: fetch_issue(repo, n, token) for n in numbers})
    text = render(problems)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(text)
    (sys.stderr if problems else sys.stdout).write(text)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
