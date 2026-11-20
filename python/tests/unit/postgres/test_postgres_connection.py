from __future__ import annotations

import builtins
import importlib
import sys
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pytest

from skewsplit.postgres.connection import connection

if TYPE_CHECKING:
    from types import ModuleType

    from typing_extensions import Self


class FakeConnection:
    def __init__(self) -> None:
        self.exits = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.exits += 1


def test_an_existing_connection_is_used_and_left_open() -> None:
    fake = FakeConnection()
    conn = cast("psycopg.Connection[Any]", fake)
    with connection(conn) as used:
        assert used is conn
    assert fake.exits == 0


def test_a_dsn_opens_an_autocommit_connection_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, dict[str, object]]] = []
    fake = FakeConnection()

    def fake_connect(conninfo: str, **kwargs: object) -> FakeConnection:
        opened.append((conninfo, kwargs))
        return fake

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with connection("host=db dbname=sales") as used:
        assert used is cast("psycopg.Connection[Any]", fake)
        assert fake.exits == 0
    assert fake.exits == 1
    assert opened == [("host=db dbname=sales", {"autocommit": True})]


def reimport_without(monkeypatch: pytest.MonkeyPatch, missing: str) -> ModuleType:
    """Import skewsplit.postgres again while ``import psycopg`` fails on ``missing``."""
    real_import = builtins.__import__

    def failing_import(name: str, *args: Any, **kwargs: Any) -> ModuleType:  # noqa: ANN401 -- as builtins.__import__
        if name == "psycopg":
            msg = f"No module named {missing!r}"
            raise ModuleNotFoundError(msg, name=missing)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "skewsplit.postgres")
    monkeypatch.setattr(builtins, "__import__", failing_import)
    return importlib.import_module("skewsplit.postgres")


def test_a_missing_driver_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ModuleNotFoundError) as caught:
        reimport_without(monkeypatch, "psycopg")
    assert str(caught.value) == (
        "skewsplit.postgres needs psycopg: pip install 'skewsplit[postgres]'"
    )
    assert caught.value.name == "psycopg"


def test_a_module_missing_inside_the_driver_keeps_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ModuleNotFoundError, match="No module named 'typing_extensions'"):
        reimport_without(monkeypatch, "typing_extensions")
