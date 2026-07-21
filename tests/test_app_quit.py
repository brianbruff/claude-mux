"""Quit tears down the owned tmux session (menu + every open Workspace).

``q`` (and the ctrl+p Quit command) should really close claude-mux rather than
leave orphaned Workspace windows behind. The kill is deferred to
``run_dashboard`` — after the Textual app has exited and restored the terminal —
so a test drives both halves: the flag set by ``action_quit`` and the
``run_dashboard`` follow-through that acts on it. A popup quit is just an overlay
close and must NOT touch the session.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from claude_mux import app as app_module
from claude_mux.app import ClaudeMuxApp, run_dashboard
from claude_mux.config import Config
from claude_mux.model import Lifecycle, Project, Worktree


def _run(coro) -> None:
    asyncio.run(coro)


def _make_projects() -> list[Project]:
    wt = Worktree(
        project_name="alpha",
        path=Path("/alpha/wt0"),
        branch="alpha-branch0",
        lifecycle=Lifecycle.DORMANT,
    )
    return [Project(name="alpha", root=Path("/alpha"), session_name="alpha", worktrees=[wt])]


def _new_app(popup: bool = False) -> ClaudeMuxApp:
    app = ClaudeMuxApp(Config(projects=[]), popup=popup)
    app.engine.snapshot = _make_projects
    return app


def test_quit_in_menu_flags_session_kill() -> None:
    """Pressing 'q' in the persistent menu marks the session for teardown."""

    async def scenario() -> None:
        app = _new_app(popup=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
        assert app._quit_kills_session is True

    _run(scenario())


def test_quit_in_popup_leaves_session_alone() -> None:
    """A popup quit is an overlay close — the owned session is left running."""

    async def scenario() -> None:
        app = _new_app(popup=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("q")
        assert app._quit_kills_session is False

    _run(scenario())


def test_run_dashboard_kills_session_when_flagged(monkeypatch) -> None:
    """run_dashboard calls tmux.kill_session iff the app flagged a full quit."""
    killed: list[bool] = []
    monkeypatch.setattr(app_module.tmux, "kill_session", lambda *a, **k: killed.append(True))

    class FakeApp:
        def __init__(self, config, popup=False) -> None:
            self._quit_kills_session = not popup

        def run(self) -> None:  # app.run() is a no-op in the test
            pass

    monkeypatch.setattr(app_module, "ClaudeMuxApp", FakeApp)

    run_dashboard(Config(projects=[]), popup=False)
    assert killed == [True]

    killed.clear()
    run_dashboard(Config(projects=[]), popup=True)
    assert killed == []  # popup never tears the session down
