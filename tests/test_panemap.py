"""Unit tests for panemap JSONL round-trip and event reading."""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_mux import panemap
from claude_mux.panemap import (
    PaneMapEntry,
    StatusEvent,
    append_event,
    append_pane_map,
    events_path,
    pane_map_path,
    read_events,
    read_pane_map,
    state_dir,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point ~/.claude at a temp dir so tests never touch the real state files."""
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


def test_state_dir_is_created_under_home(tmp_path):
    d = state_dir()
    assert d == tmp_path / ".claude" / "claude-mux"
    assert d.is_dir()


def test_pane_map_round_trip():
    append_pane_map(PaneMapEntry("%1", "sess-1", Path("/a/b"), 100.0))
    m = read_pane_map()
    assert set(m) == {"%1"}
    assert m["%1"].session_id == "sess-1"
    assert m["%1"].cwd == Path("/a/b")
    assert m["%1"].ts == 100.0


def test_pane_map_last_write_wins():
    append_pane_map(PaneMapEntry("%1", "old", Path("/a"), 1.0))
    append_pane_map(PaneMapEntry("%1", "new", Path("/b"), 2.0))
    m = read_pane_map()
    assert m["%1"].session_id == "new"
    assert m["%1"].ts == 2.0


def test_read_pane_map_missing_file_is_empty():
    assert read_pane_map() == {}


def test_read_pane_map_skips_malformed_lines():
    pane_map_path().write_text(
        "not json\n"
        '{"tmux_pane": "%2", "session_id": "s", "cwd": "/c", "ts": 5.0}\n'
        '{"missing": "keys"}\n',
        encoding="utf-8",
    )
    m = read_pane_map()
    assert set(m) == {"%2"}


def test_events_round_trip_and_none_session():
    append_event(StatusEvent("%1", None, "notification", 10.0))
    evs = read_events()
    assert len(evs) == 1
    assert evs[0].session_id is None
    assert evs[0].kind == "notification"


def test_read_events_since_filter_inclusive():
    append_event(StatusEvent("%1", "s", "start", 10.0))
    append_event(StatusEvent("%1", "s", "stop", 20.0))
    assert len(read_events()) == 2
    assert len(read_events(since_ts=20.0)) == 1
    assert read_events(since_ts=20.0)[0].kind == "stop"
    assert read_events(since_ts=21.0) == []


def test_paths():
    assert pane_map_path().name == "panes.jsonl"
    assert events_path().name == "events.jsonl"
