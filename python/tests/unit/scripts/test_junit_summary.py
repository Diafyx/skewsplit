from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import junit_summary
from junit_summary import Totals, collect, render

if TYPE_CHECKING:
    from pathlib import Path

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
<testcase classname="t.a" name="test_ok" time="0.5"/>
<testcase classname="t.a" name="test_bad" time="1.0">
  <failure message="assert 1 == 2&#10;more detail">trace</failure></testcase>
<testcase classname="t.b" name="test_err" time=""><error message="">boom</error></testcase>
<testcase classname="t.b" name="test_skip" time="0"><skipped message="later"/></testcase>
</testsuite></testsuites>
"""


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(REPORT, encoding="utf-8")
    return path


def test_collect_counts_outcomes_and_first_message_lines(report: Path) -> None:
    totals = collect([report, report])
    assert (totals.tests, totals.failed, totals.skipped) == (8, 4, 2)
    assert totals.seconds == 3.0
    assert totals.failures[:2] == ["t.a::test_bad: assert 1 == 2", "t.b::test_err"]


def test_render_passing_run() -> None:
    text = render("py-unit", Totals(tests=3, seconds=0.25))
    assert text.startswith("### ✅ py-unit\n")
    assert "| 3 | 3 | 0 | 0 | 0.2 s |" in text
    assert "Failed:" not in text


def test_render_caps_the_list_of_failures() -> None:
    names = [f"t::test_{i}" for i in range(junit_summary.MAX_LISTED + 2)]
    text = render("py-unit", Totals(tests=len(names), failed=len(names), failures=names))
    assert text.startswith("### ❌ py-unit\n")
    assert "- `t::test_0`" in text
    assert f"- `t::test_{junit_summary.MAX_LISTED}`" not in text
    assert text.endswith("- and 2 more\n")


def test_main_appends_to_the_step_summary_and_skips_missing_files(
    report: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert junit_summary.main(["py-unit", str(report), str(tmp_path / "missing.xml")]) == 0
    assert "| 4 | 1 | 2 | 1 |" in summary.read_text(encoding="utf-8")


def test_main_prints_without_a_step_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert junit_summary.main(["docs"]) == 0
    assert capsys.readouterr().out.startswith("### ✅ docs\n")
