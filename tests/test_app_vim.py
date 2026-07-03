"""Headless Pilot tests for hjkl vim navigation in the tree (M8 contract).

Drives ``ClaudeMuxApp`` via Textual's test harness to prove:
  * ``j``/``k`` move the tree cursor down/up,
  * ``l``/``h`` expand/collapse the current node,
  * none of these fire while a modal ``Input`` (BranchPrompt) is focused —
    the keystroke goes to the Input as text and the tree cursor stays put.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Input

from claude_mux.app import BranchPromptScreen, ClaudeMuxApp
from claude_mux.config import Config
from claude_mux.model import Lifecycle, Project, Worktree


def _run(coro) -> None:
    asyncio.run(coro)


def _make_projects() -> list[Project]:
    wts = [
        Worktree(
            project_name="p",
            path=Path(f"/p/wt{i}"),
            branch=f"branch{i}",
            lifecycle=Lifecycle.DORMANT,
        )
        for i in range(3)
    ]
    return [Project(name="p", root=Path("/p"), session_name="p", worktrees=wts)]


def test_jk_move_cursor() -> None:
    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        app.engine.snapshot = _make_projects
        async with app.run_test() as pilot:
            await pilot.pause()
            start = app._tree.cursor_line

            await pilot.press("j")
            await pilot.pause()
            down = app._tree.cursor_line
            assert down > start, "j did not move the cursor down"

            await pilot.press("k")
            await pilot.pause()
            assert app._tree.cursor_line == start, "k did not move the cursor up"

    _run(scenario())


def test_lh_expand_and_collapse() -> None:
    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        app.engine.snapshot = _make_projects
        async with app.run_test() as pilot:
            await pilot.pause()
            # Move onto the project node (row below the expanded root).
            await pilot.press("j")
            await pilot.pause()
            node = app._tree.cursor_node
            assert node is not None and node.allow_expand
            assert node.is_expanded  # projects render expanded

            await pilot.press("h")
            await pilot.pause()
            assert not node.is_expanded, "h did not collapse the project node"

            await pilot.press("l")
            await pilot.pause()
            assert node.is_expanded, "l did not expand the project node"

    _run(scenario())


def test_keys_do_not_fire_inside_modal_input() -> None:
    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        app.engine.snapshot = _make_projects
        async with app.run_test() as pilot:
            await pilot.pause()
            # Move onto a real row so a cursor move would be observable.
            await pilot.press("j")
            await pilot.pause()
            cursor_before = app._tree.cursor_line

            app.push_screen(BranchPromptScreen())
            await pilot.pause()
            inp = app.screen.query_one(Input)
            assert inp.has_focus

            for key in ("j", "k", "l", "h"):
                await pilot.press(key)
            await pilot.pause()

            # Letters landed in the Input as text, not as tree actions.
            assert inp.value == "jklh", f"unexpected input value: {inp.value!r}"
            assert app._tree.cursor_line == cursor_before, (
                "tree cursor moved while a modal Input was focused"
            )

    _run(scenario())
