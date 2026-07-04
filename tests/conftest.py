"""Shared pytest fixtures.

Isolates persisted Menu tree state (treestate) into a temporary directory for
every test, so the suite never reads or writes the operator's real
``~/.config/claude-mux/tree-state.json``.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_tree_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_MUX_STATE_DIR", str(tmp_path / "state"))
    yield
