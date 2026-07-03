"""Focused unit tests for claude_mux.activate orchestration.

These exercise the lifecycle wiring with tmux/git stubbed out, asserting the
safety-critical invariants: resume-command construction, lifecycle transitions,
close_workspace never touching git, and remove_worktree being the only git teardown.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_mux import activate as activate_mod
from claude_mux.config import Config
from claude_mux.model import Lifecycle, Project, SessionMeta, Worktree


class Recorder:
    """Records calls into a shared log so tests can assert ordering + args."""

    def __init__(self, log: list, git_calls: list):
        self.log = log
        self.git_calls = git_calls

    # tmux surface -------------------------------------------------
    def ensure_session(self, session_name, cwd):
        self.log.append(("ensure_session", session_name, cwd))

    def new_window(self, session_name, window_name, cwd):
        self.log.append(("new_window", session_name, window_name, cwd))
        return f"{session_name}:{window_name}"

    def build_workspace_layout(self, session_name, window_target, cwd, claude_cmd):
        self.log.append(("build_workspace_layout", session_name, window_target, cwd, claude_cmd))
        return {"claude": "%1", "yazi": "%2", "shell": "%3"}

    def jump_to(self, session_name, window_target=None, pane_id=None):
        self.log.append(("jump_to", session_name, window_target))

    def kill_window(self, session_name, window_target):
        self.log.append(("kill_window", session_name, window_target))

    # git surface --------------------------------------------------
    def sanitize_branch(self, branch):
        return branch.replace("/", "_")

    def add_worktree(self, project_root, branch, pattern, base):
        self.git_calls.append(("add_worktree", project_root, branch, pattern, base))
        return project_root.parent / f"{project_root.name}.worktrees" / branch.replace("/", "_")

    def remove_worktree(self, path):
        self.git_calls.append(("remove_worktree", path))

    # sessions surface ---------------------------------------------
    def encode_slug(self, path):
        return str(path).replace("/", "-")


@pytest.fixture
def wired(monkeypatch):
    log: list = []
    git_calls: list = []
    rec = Recorder(log, git_calls)
    for name in ("ensure_session", "new_window", "build_workspace_layout", "jump_to", "kill_window"):
        monkeypatch.setattr(activate_mod.tmux, name, getattr(rec, name))
    for name in ("sanitize_branch", "add_worktree", "remove_worktree"):
        monkeypatch.setattr(activate_mod.git, name, getattr(rec, name))
    monkeypatch.setattr(activate_mod.sessions, "encode_slug", rec.encode_slug)
    return rec


def _worktree(**kw):
    base = dict(project_name="proj", path=Path("/repo.worktrees/feat_x"), branch="feature/x")
    base.update(kw)
    return Worktree(**base)


def test_activate_fresh_when_no_session(wired):
    wt = _worktree()
    cfg = Config(projects=[], claude_cmd="claude")
    activate_mod.activate(wt, cfg)

    kinds = [c[0] for c in wired.log]
    assert kinds == ["ensure_session", "new_window", "build_workspace_layout", "jump_to"]
    layout = next(c for c in wired.log if c[0] == "build_workspace_layout")
    assert layout[4] == "claude"  # fresh, no --resume
    assert wt.lifecycle is Lifecycle.LIVE


def test_activate_resumes_latest_session(wired):
    sess = SessionMeta(
        session_id="abc-123",
        summary="s",
        first_prompt="p",
        message_count=3,
        modified=1.0,
        git_branch="feature/x",
        project_path=Path("/repo.worktrees/feat_x"),
        jsonl_path=Path("/x.jsonl"),
    )
    wt = _worktree(latest_session=sess)
    cfg = Config(projects=[], claude_cmd="claude")
    activate_mod.activate(wt, cfg)

    layout = next(c for c in wired.log if c[0] == "build_workspace_layout")
    assert layout[4] == "claude --resume abc-123"


def test_activate_no_resume_flag_ignores_session(wired):
    sess = SessionMeta("abc", "", "", 0, 1.0, "b", Path("/p"), Path("/j"))
    wt = _worktree(latest_session=sess)
    cfg = Config(projects=[], claude_cmd="claude")
    activate_mod.activate(wt, cfg, resume=False)

    layout = next(c for c in wired.log if c[0] == "build_workspace_layout")
    assert layout[4] == "claude"


def test_activate_uses_custom_claude_cmd(wired):
    wt = _worktree()
    cfg = Config(projects=[], claude_cmd="my-claude --flag")
    activate_mod.activate(wt, cfg)
    layout = next(c for c in wired.log if c[0] == "build_workspace_layout")
    assert layout[4] == "my-claude --flag"


def test_window_name_uses_sanitized_branch(wired):
    wt = _worktree(branch="feature/x")
    cfg = Config(projects=[])
    activate_mod.activate(wt, cfg)
    nw = next(c for c in wired.log if c[0] == "new_window")
    assert nw[2] == "feature_x"


def test_new_worktree_creates_then_activates(wired):
    project = Project(name="proj", root=Path("/repo"))
    cfg = Config(projects=[], base_branch="HEAD")
    wt = activate_mod.new_worktree(project, "feature/y", cfg)

    assert ("add_worktree", Path("/repo"), "feature/y", cfg.worktree_pattern, "HEAD") in wired.git_calls
    assert wt.project_name == "proj"
    assert wt.branch == "feature/y"
    assert wt.slug == str(wt.path).replace("/", "-")
    assert wt.lifecycle is Lifecycle.LIVE
    # it activated: a window was built
    assert any(c[0] == "build_workspace_layout" for c in wired.log)


def test_close_workspace_never_touches_git(wired):
    wt = _worktree(lifecycle=Lifecycle.LIVE)
    activate_mod.close_workspace(wt)

    assert wired.git_calls == []  # git untouched
    assert ("kill_window", "proj", "feature_x") in wired.log
    assert wt.lifecycle is Lifecycle.DORMANT
    assert wt.live is None


def test_close_workspace_swallows_tmux_errors(wired, monkeypatch):
    def boom(session_name, window_target):
        raise RuntimeError("no such window")

    monkeypatch.setattr(activate_mod.tmux, "kill_window", boom)
    wt = _worktree(lifecycle=Lifecycle.OPEN)
    activate_mod.close_workspace(wt)  # must not raise
    assert wt.lifecycle is Lifecycle.DORMANT


def test_remove_worktree_calls_git(wired):
    wt = _worktree(lifecycle=Lifecycle.LIVE)
    activate_mod.remove_worktree(wt)
    assert ("remove_worktree", wt.path) in wired.git_calls
    assert wt.lifecycle is Lifecycle.DORMANT
