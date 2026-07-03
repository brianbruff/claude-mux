"""Unit tests for hook install/uninstall merging and the run_hook entrypoint."""
from __future__ import annotations

import io
import json

import pytest

from claude_mux import hooks
from claude_mux.hooks import (
    EVENT_KINDS,
    install_hooks,
    run_hook,
    uninstall_hooks,
)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_creates_all_events(tmp_path):
    settings = tmp_path / "settings.json"
    changes = install_hooks(settings)
    assert len(changes) == len(EVENT_KINDS)
    data = _read(settings)
    assert set(data["hooks"]) == set(EVENT_KINDS)
    for event in EVENT_KINDS:
        cmd = data["hooks"][event][0]["hooks"][0]["command"]
        assert f"claude_mux hook {event}" in cmd


def test_install_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    install_hooks(settings)
    before = settings.read_text(encoding="utf-8")
    changes = install_hooks(settings)
    assert changes == []
    assert settings.read_text(encoding="utf-8") == before


def test_install_merges_without_clobbering_user_hooks(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo user-hook"}]}
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo lint"}],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    install_hooks(settings)
    data = _read(settings)

    # Unrelated top-level key preserved.
    assert data["model"] == "opus"
    # User's PreToolUse untouched.
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo lint"
    # User's SessionStart hook preserved AND our hook appended alongside it.
    ss_cmds = [
        h["command"]
        for group in data["hooks"]["SessionStart"]
        for h in group["hooks"]
    ]
    assert "echo user-hook" in ss_cmds
    assert any("claude_mux hook SessionStart" in c for c in ss_cmds)


def test_uninstall_removes_only_ours(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "echo user-hook"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    install_hooks(settings)
    changes = uninstall_hooks(settings)
    assert changes

    data = _read(settings)
    ss_cmds = [
        h["command"]
        for group in data["hooks"].get("SessionStart", [])
        for h in group["hooks"]
    ]
    # User's hook survives; ours is gone.
    assert ss_cmds == ["echo user-hook"]
    # Events with no surviving user hook are removed entirely.
    assert "Stop" not in data["hooks"]


def test_uninstall_missing_file_is_noop(tmp_path):
    assert uninstall_hooks(tmp_path / "nope.json") == []


def test_install_then_uninstall_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    install_hooks(settings)
    uninstall_hooks(settings)
    assert uninstall_hooks(settings) == []


# --- run_hook -------------------------------------------------------------


@pytest.fixture
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _feed_stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_run_hook_sessionstart_writes_pane_map_and_event(_isolated_home, monkeypatch):
    from claude_mux import panemap

    monkeypatch.setenv("TMUX_PANE", "%7")
    _feed_stdin(monkeypatch, json.dumps({"session_id": "abc", "cwd": "/work/dir"}))
    run_hook("SessionStart")

    pm = panemap.read_pane_map()
    assert pm["%7"].session_id == "abc"
    assert str(pm["%7"].cwd) == "/work/dir"

    evs = panemap.read_events()
    assert [e.kind for e in evs] == ["start"]
    assert evs[0].tmux_pane == "%7"


def test_run_hook_notification_maps_kind(_isolated_home, monkeypatch):
    from claude_mux import panemap

    monkeypatch.setenv("TMUX_PANE", "%1")
    _feed_stdin(monkeypatch, json.dumps({"session_id": "s"}))
    run_hook("Notification")
    evs = panemap.read_events()
    assert evs[-1].kind == "notification"
    # Notification must NOT write a pane map entry (only SessionStart does).
    assert panemap.read_pane_map() == {}


def test_run_hook_without_tmux_pane_is_silent(_isolated_home, monkeypatch):
    from claude_mux import panemap

    monkeypatch.delenv("TMUX_PANE", raising=False)
    _feed_stdin(monkeypatch, json.dumps({"session_id": "s"}))
    run_hook("SessionStart")
    assert panemap.read_pane_map() == {}
    assert panemap.read_events() == []


def test_run_hook_never_raises_on_garbage(_isolated_home, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    _feed_stdin(monkeypatch, "this is not json {{{")
    # Must not raise.
    run_hook("Stop")


def test_run_hook_swallows_broken_stdin(_isolated_home, monkeypatch):
    class Boom:
        def read(self):
            raise RuntimeError("stdin exploded")

    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr("sys.stdin", Boom())
    # Broken stdin -> no data, but still records the event without raising.
    run_hook("Stop")
