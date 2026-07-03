"""Headless Pilot tests for project management ('a' add / 'd' remove).

Every test points ``config.default_config_path`` at a temp file so the real
``~/.config/claude-mux/config.toml`` is never touched, and stands in a
``FakeEngine`` whose ``snapshot`` derives Project rows straight from the
configured project paths (no tmux/git needed to render the tree).
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from claude_mux import config as config_module
from claude_mux import picker
from claude_mux.app import ClaudeMuxApp
from claude_mux.config import Config, load_config
from claude_mux.model import Project


def _run(coro) -> None:
    asyncio.run(coro)


class FakeEngine:
    """Stand-in StatusEngine: one empty Project per configured project path."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def snapshot(self) -> list[Project]:
        return [
            Project(name=p.name, root=p, session_name=p.name, worktrees=[])
            for p in self.config.projects
        ]


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    return repo.resolve()


def _patch_env(monkeypatch, tmp_path: Path) -> Path:
    """Redirect the config path to a temp file and swap in FakeEngine."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "default_config_path", lambda: cfg)
    monkeypatch.setattr("claude_mux.app.StatusEngine", FakeEngine)
    return cfg


def test_press_a_adds_project(monkeypatch, tmp_path) -> None:
    """Pressing 'a' with a picked git repo adds it to config.toml and the tree."""
    _patch_env(monkeypatch, tmp_path)
    repo = _make_git_repo(tmp_path)

    monkeypatch.setattr(picker, "yazi_available", lambda: True)
    monkeypatch.setattr(picker, "pick_directory", lambda app, start=None: repo)

    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._projects == []

            await pilot.press("a")
            # The add now runs on a config worker thread (git + config write off
            # the UI thread); wait for it and the follow-on refresh worker to
            # finish and rebuild the tree.
            for _ in range(5):
                await app.workers.wait_for_complete()
                await pilot.pause()

            # Config file on disk carries the new project.
            reloaded = load_config()
            assert repo in [p.resolve() for p in reloaded.projects]
            # Tree shows a Project row for it.
            assert any(p.root.resolve() == repo for p in app._projects)
            names = [str(c.label) for c in app._tree.root.children]
            assert any(repo.name in n for n in names)

    _run(scenario())


def test_action_remove_project_removes_from_config(monkeypatch, tmp_path) -> None:
    """A confirmed remove drops the selected project from config + tree, and
    never deletes the folder or its git data."""
    _patch_env(monkeypatch, tmp_path)
    repo = _make_git_repo(tmp_path)

    # Seed config with the project already added.
    config_module.save_config(Config(projects=[repo]))

    # Auto-confirm any ConfirmScreen pushed by the remove action.
    async def scenario() -> None:
        app = ClaudeMuxApp(load_config())

        def auto_confirm(screen, callback=None, *args, **kwargs):
            if callback is not None:
                callback(True)

        monkeypatch.setattr(app, "push_screen", auto_confirm)

        async with app.run_test() as pilot:
            await pilot.pause()
            # Select the project node (first child under the root).
            await pilot.press("down")
            await pilot.pause()
            assert app._current_project() is not None

            app.action_remove_project()
            # The remove now runs on a config worker thread (config write off the
            # UI thread); wait for it and the follow-on refresh worker to finish
            # and rebuild the (now empty) tree.
            for _ in range(5):
                await app.workers.wait_for_complete()
                await pilot.pause()

            reloaded = load_config()
            assert repo not in [p.resolve() for p in reloaded.projects]
            assert app._projects == []

    _run(scenario())

    # (c) remove_project only edits config — the repo and its .git survive.
    assert repo.exists()
    assert (repo / ".git").exists()


def test_no_project_selected_is_a_noop(monkeypatch, tmp_path) -> None:
    """Removing with nothing selected notifies a hint and edits nothing."""
    _patch_env(monkeypatch, tmp_path)

    pushed: list[object] = []

    async def scenario() -> None:
        app = ClaudeMuxApp(Config(projects=[]))
        monkeypatch.setattr(
            app, "push_screen", lambda *a, **k: pushed.append(a)
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_remove_project()
            await pilot.pause()
            assert pushed == []  # no ConfirmScreen pushed

    _run(scenario())
