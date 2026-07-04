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


def test_collapse_survives_auto_refresh() -> None:
    """Regression: collapsing a project must not be undone by the ~4s
    auto-refresh rebuilding the tree. A background rebuild re-expanding a
    collapsed project is the 'they expand automatically after a second' bug."""
    from pathlib import Path

    from claude_mux.model import Lifecycle, Project, Worktree

    def make_projects() -> list[Project]:
        wts = [
            Worktree(project_name="p", path=Path("/p/wt0"),
                     branch="branch0", lifecycle=Lifecycle.DORMANT)
        ]
        return [Project(name="p", root=Path("/p"), session_name="p", worktrees=wts)]

    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        app.engine.snapshot = make_projects  # stable nodes across refreshes
        async with app.run_test() as pilot:
            await pilot.pause()
            pnode = app._tree.root.children[0]
            assert pnode.is_expanded
            pnode.collapse()
            await pilot.pause()
            assert not pnode.is_expanded

            app._rebuild_tree(make_projects())  # simulate auto-refresh
            await pilot.pause()

            pnode_after = app._tree.root.children[0]
            assert not pnode_after.is_expanded, "collapsed project re-expanded on refresh"

    _run(scenario())


def test_collapse_persists_across_app_restart() -> None:
    """Collapsing a project is written to disk and restored in a fresh app
    instance, so the operator's expand/collapse layout survives across
    sessions (backed by treestate)."""
    from pathlib import Path

    from claude_mux.model import Lifecycle, Project, Worktree

    def make_projects() -> list[Project]:
        wts = [
            Worktree(project_name="p", path=Path("/p/wt0"),
                     branch="branch0", lifecycle=Lifecycle.DORMANT)
        ]
        return [Project(name="p", root=Path("/p"), session_name="p", worktrees=wts)]

    async def first_session() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        app.engine.snapshot = make_projects
        async with app.run_test() as pilot:
            await pilot.pause()
            pnode = app._tree.root.children[0]
            assert pnode.is_expanded
            pnode.collapse()
            await pilot.pause()  # let the collapse event persist

    async def second_session() -> None:
        # A brand-new app instance reads the persisted state on construction.
        app = ClaudeMuxApp(Config(projects=[]))
        app.engine.snapshot = make_projects
        assert Path("/p") in app._collapsed_projects
        async with app.run_test() as pilot:
            await pilot.pause()
            pnode = app._tree.root.children[0]
            assert not pnode.is_expanded, "project not restored collapsed after restart"

    _run(first_session())
    _run(second_session())


def test_expand_clears_persisted_collapse() -> None:
    """Re-expanding a previously-collapsed project removes it from the persisted
    set, so the layout is restored expanded next time (not stuck collapsed)."""
    from pathlib import Path

    from claude_mux import treestate
    from claude_mux.model import Lifecycle, Project, Worktree

    def make_projects() -> list[Project]:
        wts = [
            Worktree(project_name="p", path=Path("/p/wt0"),
                     branch="branch0", lifecycle=Lifecycle.DORMANT)
        ]
        return [Project(name="p", root=Path("/p"), session_name="p", worktrees=wts)]

    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        app.engine.snapshot = make_projects
        async with app.run_test() as pilot:
            await pilot.pause()
            pnode = app._tree.root.children[0]
            pnode.collapse()
            await pilot.pause()
            assert Path("/p") in treestate.load_collapsed()
            pnode.expand()
            await pilot.pause()
            assert Path("/p") not in treestate.load_collapsed()

    _run(scenario())
