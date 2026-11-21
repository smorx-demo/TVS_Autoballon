"""Test isolation: general-tolerance rules and settings never touch the real data files."""

from __future__ import annotations

import pytest


def _isolate(mp: pytest.MonkeyPatch, d) -> None:
    from app.api.routes import app_settings
    from app.services.general_tolerance import store

    mp.setattr(app_settings, "JSON_PATH", d / "app_settings.json")
    mp.setattr(store, "JSON_PATH", d / "general_tolerance_rules.json")
    mp.setattr(store, "_use_pg", lambda: False)
    mp.setattr(app_settings, "_use_pg", lambda: False)


@pytest.fixture(autouse=True, scope="session")
def _session_store(tmp_path_factory):
    """Session-wide (module-scoped fixtures such as `built` run before function fixtures)."""
    mp = pytest.MonkeyPatch()
    _isolate(mp, tmp_path_factory.mktemp("gt_store"))
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolated_general_tolerance_store(tmp_path, monkeypatch):
    """Each test starts with an empty rule / settings store."""
    _isolate(monkeypatch, tmp_path)
    yield
