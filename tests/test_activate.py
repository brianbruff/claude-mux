"""Focused unit tests for claude_mux.activate orchestration (M9 / ADR-0005).

These exercise the lifecycle wiring with tmux/git stubbed out, asserting the
safety-critical invariants: the single owned session + ``<project>/<branch>``
window naming, create-or-select (no duplicate window on re-entry), resume-command
construction, lifecycle transitions, close_workspace never touching git, and
remove_worktree being the only git teardown.

The tmux surface is fully stubbed so NO real tmux server is ever touched here.
``ensure_menu_session``/``find_window`` are stubbed alongside the rest precisely
so a unit run can never create the ``claude-mux`` session on the default server.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_mux import activate as activate_mod
from claude_mux import tmux as tmux_mod
from claude_mux.config import Config
from claude_mux.model import Lifecycle, Project, SessionMeta, Worktree


class Recorder:
    """Records calls into a shared log so tests can assert ordering + args."""

    def __init__(self, log: list, git_calls: list):
        self.log = log
        self.git_calls = git_calls
        # Window name -> window id, so find_window can simulate an existing window.
        self.existing_windows: dict[str, str] = {}

    # tmux surface -------------------------------------------------
    def ensure_menu_session(self, menu_cmd=None):
        self.log.append(("ensure_menu_session",))

    def find_window(self, session_name, window_name):
        self.log.append(("find_window", session_name, window_name))
        return self.existing_windows.get(window_name)

    def new_window(self, session_name, window_name, cwd):
        self.log.append(("new_window", session_name, window_name, cwd))
        return f"@{window_name}"

    def build_workspace_layout(self, session_name, window_target, cwd, plan, launch_first=True):
        # ``plan`` is a layouts.LayoutPlan; record the injected claude command
        # (the initial pane's command) so tests can assert on it as before.
        # ``launch_first`` is recorded so tests can assert claude is DEFERRED.
        claude_cmd = plan.panes[0].command
        self.log.append(
            ("build_workspace_layout", session_name, window_target, cwd, claude_cmd, launch_first)
        )
        return {"claude": "%1", "yazi": "%2", "shell": "%3"}

    def jump_to(self, session_name, window_target=None, pane_id=None):
        self.log.append(("jump_to", session_name, window_target, pane_id))

    def launch_in_pane(self, pane_id, command, settle=0.0):
        # Claude is launched LAST (after jump_to), into the now-stable pane.
        self.log.append(("launch_in_pane", pane_id, command, settle))

    def kill_window(self, session_name, window_target):
        self.log.append(("kill_window", session_name, window_target))

    # git surface --------------------------------------------------
    def sanitize_branch(self, branch):
        # Mirror the real git.sanitize_branch: '/', '.' and ':' all break a tmux
        # session:window target, so all three are replaced.
        for ch in ("/", ".", ":"):
            branch = branch.replace(ch, "_")
        return branch

    def add_worktree(self, project_root, branch, pattern, base):
        self.git_calls.append(("add_worktree", project_root, branch, pattern, base))
        return project_root.parent / f"{project_root.name}.worktrees" / branch.replace("/", "_")

    def remove_worktree(self, path):
        self.git_calls.append(("remove_worktree", path))

    # sessions surface ---------------------------------------------
    def encode_slug(self, path):
        return str(path).replace("/", "-")


MUX = "claude-mux"


@pytest.fixture
def wired(monkeypatch):
    log: list = []
    git_calls: list = []
    rec = Recorder(log, git_calls)
    for name in (
        "ensure_menu_session",
        "find_window",
        "new_window",
        "build_workspace_layout",
        "jump_to",
        "launch_in_pane",
        "kill_window",
    ):
        monkeypatch.setattr(activate_mod.tmux, name, getattr(rec, name))
    for name in ("sanitize_branch", "add_worktree", "remove_worktree"):
        monkeypatch.setattr(activate_mod.git, name, getattr(rec, name))
    monkeypatch.setattr(activate_mod.sessions, "encode_slug", rec.encode_slug)
    return rec


def _worktree(**kw):
    base = dict(project_name="proj", path=Path("/repo.worktrees/feat_x"), branch="feature/x")
    base.update(kw)
    return Worktree(**base)


def test_activate_fresh_when_no_window(wired):
    wt = _worktree()
    cfg = Config(projects=[], claude_cmd="claude")
    activate_mod.activate(wt, cfg)

    kinds = [c[0] for c in wired.log]
    # Claude is launched LAST, after jump_to full-screens the window and selects the
    # pane — so its startup handshake runs in a quiet, flushed pty (no 0n0n leak).
    assert kinds == [
        "ensure_menu_session",
        "find_window",
        "new_window",
        "build_workspace_layout",
        "jump_to",
        "launch_in_pane",
    ]
    # Everything targets the single owned session.
    nw = next(c for c in wired.log if c[0] == "new_window")
    assert nw[1] == MUX
    layout = next(c for c in wired.log if c[0] == "build_workspace_layout")
    assert layout[4] == "claude"  # fresh, no --resume
    assert layout[5] is False  # claude launch DEFERRED out of build_workspace_layout
    # Final jump selects the workspace window AND the claude pane, no switch-client.
    # The window target is the id captured from new_window (name-derived), whatever
    # the exact sanitized+disambiguated name is.
    name = activate_mod._workspace_window_name("proj", wt)
    jump = next(c for c in wired.log if c[0] == "jump_to")
    assert jump == ("jump_to", MUX, f"@{name}", "%1")
    # ...and claude launches into that same pane, last, with a settle delay.
    launch = next(c for c in wired.log if c[0] == "launch_in_pane")
    assert launch[1] == "%1" and launch[2] == "claude"
    assert launch[3] == tmux_mod.CLAUDE_LAUNCH_SETTLE
    assert wt.lifecycle is Lifecycle.LIVE


def test_open_or_select_selects_existing_no_duplicate(wired):
    # A window for this worktree already exists -> select it, never create a new
    # window or launch a second claude.
    wt = _worktree()
    project = Project(name="proj", root=Path("/repo"))
    wired.existing_windows[activate_mod._workspace_window_name("proj", wt)] = "@already"
    cfg = Config(projects=[], claude_cmd="claude")

    target = activate_mod.open_or_select_workspace(project, wt, cfg)

    assert target == "@already"
    kinds = [c[0] for c in wired.log]
    assert kinds == ["ensure_menu_session", "find_window", "jump_to"]
    assert not any(c[0] == "new_window" for c in wired.log)
    assert not any(c[0] == "build_workspace_layout" for c in wired.log)
    jump = next(c for c in wired.log if c[0] == "jump_to")
    assert jump == ("jump_to", MUX, "@already", None)


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


def test_activate_pins_model_when_configured(wired):
    wt = _worktree()
    cfg = Config(projects=[], claude_cmd="claude", model="opus")
    activate_mod.activate(wt, cfg)
    layout = next(c for c in wired.log if c[0] == "build_workspace_layout")
    assert layout[4] == "claude --model opus"


def test_activate_model_and_resume_compose(wired):
    sess = SessionMeta("abc-123", "", "", 0, 1.0, "b", Path("/p"), Path("/j"))
    wt = _worktree(latest_session=sess)
    cfg = Config(projects=[], claude_cmd="claude", model="opus")
    activate_mod.activate(wt, cfg)
    layout = next(c for c in wired.log if c[0] == "build_workspace_layout")
    assert layout[4] == "claude --model opus --resume abc-123"


def test_window_name_is_project_slash_branch(wired):
    wt = _worktree(branch="feature/x")
    cfg = Config(projects=[])
    activate_mod.activate(wt, cfg)
    nw = next(c for c in wired.log if c[0] == "new_window")
    # UI-only grouping: <project>/<sanitized-branch>-<path-digest>. The branch is
    # sanitized (so a '.'/':' cannot make session:<name> misparse) and a stable
    # per-path digest suffix keeps the name unique across same-basename Projects.
    assert nw[2].startswith("proj/feature_x-")
    assert "/" not in nw[2].split("/", 1)[1]  # no raw slash survives in the branch part
    assert nw[2] == activate_mod._workspace_window_name("proj", wt)


def test_window_name_sanitizes_dotted_branch(wired):
    # A branch like 'release-2.5' must not leave a '.' in the window name, or
    # kill_window's 'session:<name>' target would misparse '.5' as a pane index
    # and the window would leak while the worktree is flipped DORMANT.
    wt = _worktree(branch="release-2.5")
    name = activate_mod._workspace_window_name("proj", wt)
    assert "." not in name
    assert name.startswith("proj/release-2_5-")


def test_window_name_unique_across_same_basename_projects():
    # Two configured repos share a directory basename ('api') and hold the same
    # branch ('main'); their Worktrees live at different paths, so the window
    # names must differ (else find_window dedup selects the wrong project's window).
    wt_a = Worktree(project_name="api", path=Path("/home/a/api"), branch="main")
    wt_b = Worktree(project_name="api", path=Path("/home/b/api"), branch="main")
    assert activate_mod._workspace_window_name("api", wt_a) != activate_mod._workspace_window_name("api", wt_b)


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
    # Kills the worktree's window in the owned session; menu window 0 survives.
    # close_workspace must derive the SAME name open used (find/create/kill agree).
    name = activate_mod._workspace_window_name(wt.project_name, wt)
    assert ("kill_window", MUX, name) in wired.log
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
