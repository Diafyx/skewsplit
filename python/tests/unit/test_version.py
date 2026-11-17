from __future__ import annotations

import runpy
import sys
from importlib.metadata import version

import pytest

import skewsplit
from skewsplit.cli import main


def test_version_matches_package_metadata() -> None:
    assert skewsplit.__version__ == version("skewsplit")


def test_cli_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"skewsplit {skewsplit.__version__}\n"


def test_cli_without_arguments_succeeds() -> None:
    assert main([]) == 0


def test_cli_rejects_unknown_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--no-such-flag"])
    assert exit_info.value.code == 2
    assert "unrecognized arguments: --no-such-flag" in capsys.readouterr().err


def test_python_dash_m_runs_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["skewsplit"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("skewsplit", run_name="__main__")
    assert exit_info.value.code == 0
