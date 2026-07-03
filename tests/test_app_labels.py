"""Unit tests for the pure label-formatting helpers in claude_mux.app.

These functions carry the only branching logic in the appcli group and are
side-effect free, so they are testable without constructing the Textual App
(which would need a real StatusEngine).
"""
from __future__ import annotations

from pathlib import Path

from claude_mux.app import (
    activity_text,
    format_idle,
    project_label,
    scrape_extras,
    worktree_label,
    worktree_summary,
)
from claude_mux.model import (
    Activity,
    Lifecycle,
    LiveClaude,
    Project,
    SessionMeta,
    Worktree,
)


def _live(**kw) -> LiveClaude:
    base = dict(
        pane_id="%1",
        session_name="proj",
        window_index=0,
        pid=123,
        cwd=Path("/tmp/x"),
    )
    base.update(kw)
    return LiveClaude(**base)


# -- format_idle ------------------------------------------------------------ #


def test_format_idle_none():
    assert format_idle(None) is None


def test_format_idle_seconds():
    assert format_idle(5) == "idle 5s"
    assert format_idle(59) == "idle 59s"


def test_format_idle_minutes():
    assert format_idle(60) == "idle 1m"
    assert format_idle(185) == "idle 3m"


def test_format_idle_hours():
    assert format_idle(3600) == "idle 1h"


def test_format_idle_negative_clamped():
    assert format_idle(-10) == "idle 0s"


# -- activity_text ---------------------------------------------------------- #


def test_activity_text_no_live():
    assert activity_text(None) == ""


def test_activity_text_running_waiting():
    assert activity_text(_live(activity=Activity.RUNNING)) == "running"
    assert activity_text(_live(activity=Activity.WAITING)) == "waiting"


def test_activity_text_idle_uses_age():
    assert activity_text(_live(activity=Activity.IDLE, idle_seconds=120)) == "idle 2m"


def test_activity_text_idle_without_age():
    assert activity_text(_live(activity=Activity.IDLE)) == "idle"


def test_activity_text_unknown_is_blank():
    assert activity_text(_live(activity=Activity.UNKNOWN)) == ""


# -- scrape_extras ---------------------------------------------------------- #


def test_scrape_extras_empty_when_no_live():
    assert scrape_extras(None) == []


def test_scrape_extras_only_present_fields():
    live = _live(model="Opus 4.8", context_pct=5, cost_usd=1.04, elapsed="2m 59s")
    assert scrape_extras(live) == ["Opus 4.8", "5%", "$1.04", "2m 59s"]


def test_scrape_extras_partial():
    assert scrape_extras(_live(cost_usd=0.0)) == ["$0.00"]
    assert scrape_extras(_live(context_pct=0)) == ["0%"]  # 0 is a real value, not None


# -- worktree_summary ------------------------------------------------------- #


def _session(summary: str) -> SessionMeta:
    return SessionMeta(
        session_id="s1",
        summary=summary,
        first_prompt="",
        message_count=1,
        modified=0.0,
        git_branch="main",
        project_path=Path("/tmp/x"),
        jsonl_path=Path("/tmp/x.jsonl"),
    )


def _wt(**kw) -> Worktree:
    base = dict(project_name="proj", path=Path("/tmp/proj"), branch="main")
    base.update(kw)
    return Worktree(**base)


def test_worktree_summary_prefers_live():
    wt = _wt(
        live=_live(summary="from live"),
        latest_session=_session("from session"),
    )
    assert worktree_summary(wt) == "from live"


def test_worktree_summary_falls_back_to_session():
    wt = _wt(latest_session=_session("from session"))
    assert worktree_summary(wt) == "from session"


def test_worktree_summary_empty():
    assert worktree_summary(_wt()) == ""


# -- worktree_label --------------------------------------------------------- #


def test_worktree_label_dormant_minimal():
    label = worktree_label(_wt(branch="feature/x", lifecycle=Lifecycle.DORMANT))
    assert label.startswith("○ ")
    assert "feature/x" in label
    # No activity/summary/extras -> just the marker + name.
    assert "   " not in label


def test_worktree_label_primary_marked():
    label = worktree_label(_wt(branch="main", is_primary=True))
    assert "main *" in label


def test_worktree_label_live_full():
    wt = _wt(
        branch="feat",
        lifecycle=Lifecycle.LIVE,
        live=_live(
            activity=Activity.WAITING,
            summary="doing a thing",
            model="Opus 4.8",
            context_pct=5,
            cost_usd=1.04,
        ),
    )
    label = worktree_label(wt)
    assert label.startswith("● ")
    assert "waiting" in label
    assert "doing a thing" in label
    assert "Opus 4.8" in label
    assert "$1.04" in label


def test_worktree_label_open_marker():
    label = worktree_label(_wt(lifecycle=Lifecycle.OPEN))
    assert label.startswith("◐ ")


# -- project_label ---------------------------------------------------------- #


def test_project_label_pluralization():
    p0 = Project(name="p", root=Path("/tmp/p"))
    assert "0 worktrees" in project_label(p0)
    p1 = Project(name="p", root=Path("/tmp/p"), worktrees=[_wt()])
    assert "1 worktree)" in project_label(p1)
    p2 = Project(name="p", root=Path("/tmp/p"), worktrees=[_wt(), _wt()])
    assert "2 worktrees" in project_label(p2)
