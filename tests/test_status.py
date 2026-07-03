"""Unit tests for StatusEngine snapshot assembly (dependencies monkeypatched)."""
from __future__ import annotations

import time
from pathlib import Path

from claude_mux import git, panemap, scrape, sessions, status, tmux
from claude_mux.config import Config
from claude_mux.model import Activity, Lifecycle, SessionMeta, Worktree
from claude_mux.panemap import PaneMapEntry, StatusEvent
from claude_mux.scrape import ScrapeResult
from claude_mux.tmux import PaneInfo


def _pane(pane_id, path, cmd="claude", session="proj", widx=1, pid=100):
    return PaneInfo(
        pane_id=pane_id,
        session_name=session,
        window_index=widx,
        pane_index=0,
        current_command=cmd,
        pid=pid,
        current_path=Path(path),
    )


def _wt(path, branch="main", primary=False, slug="slug"):
    return Worktree(
        project_name="proj",
        path=Path(path),
        branch=branch,
        is_primary=primary,
        slug=slug,
    )


def _install(monkeypatch, *, worktrees, panes, pane_map=None, events=None,
             index=None, footer=None):
    monkeypatch.setattr(git, "list_worktrees", lambda root: list(worktrees))
    monkeypatch.setattr(tmux, "list_panes", lambda: list(panes))
    monkeypatch.setattr(tmux, "is_claude_command", lambda c: c in ("claude", "node"))
    monkeypatch.setattr(tmux, "capture_pane", lambda pid, lines=8: "footer")
    monkeypatch.setattr(panemap, "read_pane_map", lambda: dict(pane_map or {}))
    monkeypatch.setattr(panemap, "read_events", lambda since_ts=0.0: list(events or []))
    monkeypatch.setattr(sessions, "read_session_index", lambda slug: list(index or []))
    monkeypatch.setattr(
        scrape, "parse_footer",
        lambda cap: footer or ScrapeResult(None, None, None, None, None, None, None),
    )


def test_dormant_when_no_pane(monkeypatch, tmp_path):
    wt = _wt(tmp_path / "wt")
    _install(monkeypatch, worktrees=[wt], panes=[])
    eng = status.StatusEngine(Config(projects=[tmp_path]))
    projects = eng.snapshot()
    assert len(projects) == 1
    assert projects[0].worktrees[0].lifecycle is Lifecycle.DORMANT
    assert projects[0].worktrees[0].live is None


def test_open_when_nonclaude_pane_present(monkeypatch, tmp_path):
    wtdir = tmp_path / "wt"
    wt = _wt(wtdir)
    _install(monkeypatch, worktrees=[wt], panes=[_pane("%1", wtdir, cmd="zsh")])
    eng = status.StatusEngine(Config(projects=[tmp_path]))
    wt_out = eng.snapshot()[0].worktrees[0]
    assert wt_out.lifecycle is Lifecycle.OPEN
    assert wt_out.live is None


def test_live_identity_from_pane_map(monkeypatch, tmp_path):
    wtdir = tmp_path / "wt"
    wt = _wt(wtdir, slug="the-slug")
    meta = SessionMeta(
        session_id="AUTH", summary="from-map", first_prompt="p", message_count=3,
        modified=time.time(), git_branch="main",
        project_path=wtdir, jsonl_path=tmp_path / "a.jsonl",
    )
    _install(
        monkeypatch,
        worktrees=[wt],
        panes=[_pane("%9", wtdir)],
        pane_map={"%9": PaneMapEntry("%9", "AUTH", wtdir, 1.0)},
        events=[StatusEvent("%9", "AUTH", "start", 5.0)],
        index=[meta],
    )
    eng = status.StatusEngine(Config(projects=[tmp_path]))
    wt_out = eng.snapshot()[0].worktrees[0]
    assert wt_out.lifecycle is Lifecycle.LIVE
    assert wt_out.live.session_id == "AUTH"
    assert wt_out.live.summary == "from-map"
    assert wt_out.live.activity is Activity.RUNNING


def test_live_identity_heuristic_newest_session(monkeypatch, tmp_path):
    wtdir = tmp_path / "wt"
    wt = _wt(wtdir, slug="the-slug")
    old = SessionMeta("OLD", "old", "p", 1, 10.0, "main", wtdir, tmp_path / "o.jsonl")
    new = SessionMeta("NEW", "new", "p", 1, 20.0, "main", wtdir, tmp_path / "n.jsonl")
    _install(monkeypatch, worktrees=[wt], panes=[_pane("%2", wtdir)], index=[old, new])
    eng = status.StatusEngine(Config(projects=[tmp_path]))
    live = eng.snapshot()[0].worktrees[0].live
    assert live.session_id == "NEW"
    assert live.summary == "new"


def test_waiting_event_beats_scrape(monkeypatch, tmp_path):
    wtdir = tmp_path / "wt"
    wt = _wt(wtdir)
    _install(
        monkeypatch,
        worktrees=[wt],
        panes=[_pane("%3", wtdir)],
        events=[StatusEvent("%3", None, "notification", 1.0)],
    )
    eng = status.StatusEngine(Config(projects=[tmp_path]))
    assert eng.snapshot()[0].worktrees[0].live.activity is Activity.WAITING


def test_scrape_waiting_fallback_and_extras(monkeypatch, tmp_path):
    wtdir = tmp_path / "wt"
    wt = _wt(wtdir)
    footer = ScrapeResult(
        model="Opus 4.8", project="proj", branch="main",
        context_pct=5, cost_usd=1.04, elapsed="2m 59s", waiting=True,
    )
    _install(monkeypatch, worktrees=[wt], panes=[_pane("%4", wtdir)], footer=footer)
    eng = status.StatusEngine(Config(projects=[tmp_path]))
    live = eng.snapshot()[0].worktrees[0].live
    assert live.activity is Activity.WAITING  # no event -> scrape fallback
    assert live.model == "Opus 4.8"
    assert live.context_pct == 5
    assert live.cost_usd == 1.04
    assert live.elapsed == "2m 59s"


def test_dependency_failure_degrades_gracefully(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(git, "list_worktrees", boom)
    monkeypatch.setattr(tmux, "list_panes", lambda: [])
    monkeypatch.setattr(tmux, "is_claude_command", lambda c: False)
    monkeypatch.setattr(panemap, "read_pane_map", lambda: {})
    monkeypatch.setattr(panemap, "read_events", lambda since_ts=0.0: [])
    eng = status.StatusEngine(Config(projects=[tmp_path]))
    projects = eng.snapshot()
    assert len(projects) == 1
    assert projects[0].worktrees == []
