"""Headless smoke test: mount the dashboard app with Textual's test harness.

Uses ``App.run_test`` / ``Pilot`` to prove ``ClaudeMuxApp`` composes and reaches
a running state with no live claude, no tmux server, and no configured projects.
We drive the async context manager via ``asyncio.run`` so the suite needs no
``pytest-asyncio`` plugin.
"""
from __future__ import annotations

import asyncio

from textual.widgets import Footer, Header, Tree

from claude_mux.app import ClaudeMuxApp
from claude_mux.config import Config


def _run(coro) -> None:
    asyncio.run(coro)


def test_app_composes_with_no_projects() -> None:
    """The app mounts, builds its widget tree, and survives a refresh cycle."""

    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        async with app.run_test() as pilot:
            # Let on_mount + the first background refresh worker settle.
            await pilot.pause()

            # Core chrome composed.
            assert app.query_one(Header) is not None
            assert app.query_one(Footer) is not None

            tree = app.query_one(Tree)
            assert tree is app._tree
            assert str(tree.root.label) == "Projects"

            # Empty config -> a snapshot with no Project rows, app still alive.
            assert app._projects == []
            assert len(tree.root.children) == 0
            assert app.is_running

    _run(scenario())


def test_app_composes_in_popup_mode() -> None:
    """Popup mode composes identically and records its popup sub-title."""

    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]), popup=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.popup is True
            assert app.sub_title == "popup"
            assert app.query_one(Tree) is not None

    _run(scenario())


def test_cursor_survives_auto_refresh() -> None:
    """Regression: navigating into a worktree must not be undone by the ~4s
    auto-refresh rebuilding the tree. The cursor is restored to the same
    worktree so the operator can actually resume/activate it."""
    from pathlib import Path

    from claude_mux.model import Lifecycle, Project, Worktree

    def make_projects() -> list[Project]:
        wts = [
            Worktree(project_name="p", path=Path(f"/p/wt{i}"),
                     branch=f"branch{i}", lifecycle=Lifecycle.DORMANT)
            for i in range(6)
        ]
        return [Project(name="p", root=Path("/p"), session_name="p", worktrees=wts)]

    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        app.engine.snapshot = make_projects  # stable nodes across refreshes
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(4):
                await pilot.press("down")
            await pilot.pause()
            before = app._tree.cursor_node.data
            assert isinstance(before, Worktree)

            app._rebuild_tree(make_projects())  # simulate auto-refresh
            await pilot.pause()
            await pilot.pause()  # let call_after_refresh fire

            after = app._tree.cursor_node.data
            assert isinstance(after, Worktree), "cursor fell off the worktree row"
            assert after.path == before.path, "cursor jumped to a different row"

    _run(scenario())
