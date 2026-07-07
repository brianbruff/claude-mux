"""Headless Pilot tests for hjkl LEVEL navigation in the tree (M8 contract).

Drives ``ClaudeMuxApp`` via Textual's test harness to prove:
  * ``j``/``k`` move between SIBLINGS at the current level — project↔project and
    worktree↔worktree of the same project (not over every visible row),
  * ``l`` steps IN (project → its first worktree; worktree → resume its session),
  * ``h`` steps OUT (worktree → its parent project),
  * ``o`` opens the selected worktree in the configured editor,
  * none of these fire while a modal ``Input`` (BranchPrompt) is focused — the
    keystroke goes to the Input as text and the tree cursor stays put.
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


def _project(name: str, n_worktrees: int) -> Project:
    wts = [
        Worktree(
            project_name=name,
            path=Path(f"/{name}/wt{i}"),
            branch=f"{name}-branch{i}",
            lifecycle=Lifecycle.DORMANT,
        )
        for i in range(n_worktrees)
    ]
    return Project(name=name, root=Path(f"/{name}"), session_name=name, worktrees=wts)


def _make_projects() -> list[Project]:
    """Two projects, so sibling-level moves are observable."""
    return [_project("alpha", 3), _project("beta", 2)]


def _new_app() -> ClaudeMuxApp:
    app = ClaudeMuxApp(Config(projects=[]))
    app.engine.snapshot = _make_projects
    return app


async def _step_onto_first_project(app, pilot) -> None:
    """From the root, ``j`` drops onto the first project row."""
    await pilot.press("j")
    await pilot.pause()


def test_jk_move_between_sibling_projects() -> None:
    async def scenario() -> None:
        app = _new_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _step_onto_first_project(app, pilot)
            node = app._tree.cursor_node
            assert isinstance(node.data, Project) and node.data.name == "alpha"

            await pilot.press("j")
            await pilot.pause()
            node = app._tree.cursor_node
            assert isinstance(node.data, Project) and node.data.name == "beta", (
                "j did not step to the next sibling project"
            )

            # At the last project, j is a no-op (no next sibling).
            await pilot.press("j")
            await pilot.pause()
            assert app._tree.cursor_node.data.name == "beta"

            await pilot.press("k")
            await pilot.pause()
            assert app._tree.cursor_node.data.name == "alpha", (
                "k did not step to the previous sibling project"
            )

    _run(scenario())


def test_l_steps_into_worktrees_and_jk_stay_within_project() -> None:
    async def scenario() -> None:
        app = _new_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _step_onto_first_project(app, pilot)  # on 'alpha'

            await pilot.press("l")  # step IN → first worktree
            await pilot.pause()
            node = app._tree.cursor_node
            assert isinstance(node.data, Worktree)
            assert node.data.branch == "alpha-branch0"

            await pilot.press("j")  # next sibling worktree
            await pilot.pause()
            assert app._tree.cursor_node.data.branch == "alpha-branch1"

            await pilot.press("k")  # previous sibling worktree
            await pilot.pause()
            assert app._tree.cursor_node.data.branch == "alpha-branch0"

    _run(scenario())


def test_h_steps_out_from_worktree_to_project() -> None:
    async def scenario() -> None:
        app = _new_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _step_onto_first_project(app, pilot)  # on 'alpha'
            await pilot.press("l")  # into worktrees
            await pilot.pause()
            assert isinstance(app._tree.cursor_node.data, Worktree)

            await pilot.press("h")  # step OUT
            await pilot.pause()
            node = app._tree.cursor_node
            assert isinstance(node.data, Project) and node.data.name == "alpha", (
                "h did not step out to the parent project"
            )

    _run(scenario())


def test_l_on_worktree_resumes_session() -> None:
    async def scenario() -> None:
        app = _new_app()
        opened: list[tuple[Worktree, bool]] = []
        # Intercept the lifecycle primitive so no tmux is touched.
        app._jump_or_activate = lambda wt: opened.append((wt, True))
        async with app.run_test() as pilot:
            await pilot.pause()
            await _step_onto_first_project(app, pilot)
            await pilot.press("l")  # into first worktree
            await pilot.pause()
            # l is intercepted at the worktree level only after descent; descent
            # itself does not open. Now press l again on the worktree.
            wt = app._tree.cursor_node.data
            assert isinstance(wt, Worktree)

            await pilot.press("l")  # resume the worktree
            await pilot.pause()
            assert opened == [(wt, True)], "l on a worktree did not resume it"

    _run(scenario())


def test_o_opens_worktree_in_editor() -> None:
    async def scenario() -> None:
        app = _new_app()
        opened: list[Worktree] = []
        app._do_open_editor = lambda wt: opened.append(wt)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _step_onto_first_project(app, pilot)
            await pilot.press("l")  # into first worktree
            await pilot.pause()
            wt = app._tree.cursor_node.data

            await pilot.press("o")
            await pilot.pause()
            assert opened == [wt], "o did not open the worktree in the editor"

    _run(scenario())


def test_keys_do_not_fire_inside_modal_input() -> None:
    async def scenario() -> None:
        app = _new_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            # Move onto a real row so a cursor move would be observable.
            await pilot.press("j")
            await pilot.pause()
            cursor_before = app._tree.cursor_line

            app.push_screen(BranchPromptScreen(["main", "develop"], "develop"))
            await pilot.pause()
            inp = app.screen.query_one(Input)
            assert inp.has_focus

            for key in ("j", "k", "l", "h", "o"):
                await pilot.press(key)
            await pilot.pause()

            # Letters landed in the Input as text, not as tree actions.
            assert inp.value == "jklho", f"unexpected input value: {inp.value!r}"
            assert app._tree.cursor_line == cursor_before, (
                "tree cursor moved while a modal Input was focused"
            )

    _run(scenario())


def test_branch_prompt_returns_branch_and_selected_base() -> None:
    """Submitting yields (branch, base); the base defaults to the preselected one
    and follows the dropdown when changed."""
    from textual.widgets import Select

    async def scenario() -> None:
        app = _new_app()
        results: list = []
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(
                BranchPromptScreen(["main", "develop", "master"], "develop"),
                results.append,
            )
            await pilot.pause()

            # Default base is the preselected develop.
            assert app.screen.query_one("#base", Select).value == "develop"

            # Type a branch, switch the base to main (a hotfix off trunk), submit.
            app.screen.query_one(Input).value = "hotfix/urgent"
            app.screen.query_one("#base", Select).value = "main"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

        assert results == [("hotfix/urgent", "main")]

    _run(scenario())


def test_branch_prompt_offers_fallback_base_not_in_branches() -> None:
    """A default_base absent from the branch list (e.g. the HEAD fallback) is still
    selectable and preselected."""
    from textual.widgets import Select

    async def scenario() -> None:
        app = _new_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(BranchPromptScreen([], "HEAD"))
            await pilot.pause()
            sel = app.screen.query_one("#base", Select)
            assert sel.value == "HEAD"

    _run(scenario())
