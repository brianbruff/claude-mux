"""Unit tests for persisted Menu tree state (treestate).

Collapse state is disposable view state: a missing or corrupt file yields an
empty set (everything expanded) rather than an error.
"""
from __future__ import annotations

from pathlib import Path

from claude_mux import treestate


def test_load_missing_file_returns_empty_set(tmp_path: Path):
    assert treestate.load_collapsed(tmp_path / "nope.json") == set()


def test_save_load_round_trip(tmp_path: Path):
    state = tmp_path / "tree-state.json"
    collapsed = {Path("/p/one"), Path("/p/two")}
    treestate.save_collapsed(collapsed, state)
    assert treestate.load_collapsed(state) == collapsed


def test_save_creates_parent_dir(tmp_path: Path):
    state = tmp_path / "nested" / "dir" / "tree-state.json"
    treestate.save_collapsed({Path("/p")}, state)
    assert state.exists()


def test_load_corrupt_file_returns_empty_set(tmp_path: Path):
    state = tmp_path / "tree-state.json"
    state.write_text("{ not valid json", encoding="utf-8")
    assert treestate.load_collapsed(state) == set()


def test_save_empty_set_round_trips(tmp_path: Path):
    state = tmp_path / "tree-state.json"
    treestate.save_collapsed(set(), state)
    assert treestate.load_collapsed(state) == set()


def test_env_override_directs_default_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_MUX_STATE_DIR", str(tmp_path / "custom"))
    assert treestate.default_state_path() == tmp_path / "custom" / "tree-state.json"
