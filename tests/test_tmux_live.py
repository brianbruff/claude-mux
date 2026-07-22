"""Live-tmux tests for the M9 encapsulated single-session model (ADR-0005).

These spin up a REAL tmux server on a PRIVATE socket (``tmux -L <uniq>``) and
monkeypatch ``tmux.MUX_SESSION`` to a throwaway name, so nothing here can ever
touch the operator's default server or their real ``claude-mux`` session.

They assert the M9 contract end-to-end:
  * ``bootstrap`` creates window 0 named ``menu``;
  * ``open_or_select_workspace`` creates the ``<project>/<branch>`` window, makes
    it the active window, and selects the (left) claude pane;
  * a second call SELECTS the existing window (no duplicate);
  * the back-to-menu binding is registered and ``select-window :menu`` works;
  * ``close_workspace`` kills the workspace window while the menu survives.

Safety rules (task LIVE-TMUX SAFETY):
  * PRIVATE socket only; ``tmux -L <sock> kill-server`` in teardown + stale-socket
    removal;
  * ``MUX_SESSION`` monkeypatched to a test name;
  * never launch real ``claude`` — an inert ``sleep 300`` stands in for it and for
    the menu window's process.
Skipped entirely when tmux is not installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from claude_mux import activate, tmux
from claude_mux.config import Config
from claude_mux.model import Lifecycle, Project, Worktree

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

INERT = "sleep 300"  # stands in for both claude and the menu process


@pytest.fixture
def mux(monkeypatch):
    """Private tmux socket + throwaway MUX_SESSION; killed + cleaned in teardown."""
    sock = f"cmuxt{uuid.uuid4().hex[:8]}"
    session = f"cmuxsess{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(tmux, "_SOCKET_NAME", sock)
    monkeypatch.setattr(tmux, "MUX_SESSION", session)
    try:
        yield sock, session
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
        # Remove the stale socket file the dead server leaves behind.
        for base in (f"/private/tmp/tmux-{os.getuid()}", f"/tmp/tmux-{os.getuid()}"):
            p = Path(base) / sock
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def _q(sock: str, *args: str) -> str:
    """Run a tmux command on the private socket and return trimmed stdout."""
    out = subprocess.run(
        ["tmux", "-L", sock, *args], capture_output=True, text=True
    )
    return out.stdout.strip()


def _active_window_name(sock: str, session: str) -> str:
    return _q(sock, "display-message", "-p", "-t", session, "#{window_name}")


def _window_names(sock: str, session: str) -> list[str]:
    out = _q(sock, "list-windows", "-t", session, "-F", "#{window_name}")
    return [n for n in out.splitlines() if n]


def _worktree(session: str) -> Worktree:
    # branch has a slash on purpose: exercises the '<project>/<branch>' name.
    return Worktree(
        project_name="proj",
        path=Path.cwd(),
        branch="feature/live",
        lifecycle=Lifecycle.DORMANT,
    )


def test_bootstrap_creates_menu_window(mux):
    sock, session = mux
    # attach=False: build the session without placing a client (no TTY in tests).
    tmux.bootstrap(menu_cmd=INERT, attach=False)

    server = tmux._server()
    assert server.has_session(session)
    # Window 0 is the menu.
    assert _q(sock, "display-message", "-p", "-t", f"{session}:0", "#{window_name}") == "menu"
    assert tmux.find_window(session, "menu") is not None
    # Idempotent: a second bootstrap does not create a duplicate menu window.
    tmux.bootstrap(menu_cmd=INERT, attach=False)
    assert _window_names(sock, session).count("menu") == 1


def _pane_count(sock: str, window_target: str) -> int:
    out = _q(sock, "list-panes", "-t", window_target, "-F", "#{pane_id}")
    return len([p for p in out.splitlines() if p])


def _prefix_binding(sock: str, key: str) -> str:
    out = _q(sock, "list-keys", "-T", "prefix")
    for line in out.splitlines():
        parts = line.split(maxsplit=4)
        if len(parts) >= 4 and parts[:4] == ["bind-key", "-T", "prefix", key]:
            return line
    return ""



def test_bootstrap_adds_terminal_beside_menu(mux):
    sock, session = mux
    tmux.bootstrap(menu_cmd=INERT, attach=False)

    # The menu window carries two panes: the Textual tree (left) + a shell (right).
    assert _pane_count(sock, f"{session}:menu") == 2
    # The left pane (the tree) stays focused so the operator drives the menu.
    assert _q(sock, "display-message", "-p", "-t", session, "#{pane_left}") == "0"
    # Idempotent: re-running bootstrap does not stack up extra terminals.
    tmux.bootstrap(menu_cmd=INERT, attach=False)
    tmux.ensure_menu_terminal()
    assert _pane_count(sock, f"{session}:menu") == 2


def test_open_or_select_creates_and_selects_workspace(mux):
    sock, session = mux
    tmux.bootstrap(menu_cmd=INERT, attach=False)

    project = Project(name="proj", root=Path.cwd())
    wt = _worktree(session)
    cfg = Config(projects=[], claude_cmd=INERT)

    target = activate.open_or_select_workspace(project, wt, cfg)

    name = activate._workspace_window_name("proj", wt)
    # The freshly-built workspace window is the active window, full-screen.
    assert _active_window_name(sock, session) == name
    assert wt.lifecycle is Lifecycle.LIVE
    # find_window resolves the same window id we selected.
    assert tmux.find_window(session, name) == target
    # The claude (left column) pane is the active pane.
    assert _q(sock, "display-message", "-p", "-t", session, "#{pane_left}") == "0"
    assert _pane_count(sock, target) == 4


def test_second_open_selects_existing_no_duplicate(mux):
    sock, session = mux
    tmux.bootstrap(menu_cmd=INERT, attach=False)

    project = Project(name="proj", root=Path.cwd())
    wt = _worktree(session)
    cfg = Config(projects=[], claude_cmd=INERT)

    name = activate._workspace_window_name("proj", wt)
    first = activate.open_or_select_workspace(project, wt, cfg)
    # Move focus back to the menu so the second call has to re-select.
    _q(sock, "select-window", "-t", f"{session}:menu")
    assert _active_window_name(sock, session) == "menu"

    second = activate.open_or_select_workspace(project, wt, cfg)

    assert second == first  # same window id
    assert _window_names(sock, session).count(name) == 1
    assert _active_window_name(sock, session) == name


def test_menu_keybinding_registered_and_works(mux):
    sock, session = mux
    tmux.bootstrap(menu_cmd=INERT, attach=False)

    # The binding is registered under the prefix table. It is session-guarded via
    # if-shell (tmux key tables are server-global), so the menu jump only fires in
    # the owned session and other sessions keep the built-in mark-pane.
    keys = _prefix_binding(sock, "m")
    assert "select-window" in keys
    assert f"{session}:menu" in keys
    assert "if-shell" in keys
    assert "select-pane -m" in keys  # mark-pane preserved for other sessions

    # And the underlying select-window target works: from a workspace, jump back.
    project = Project(name="proj", root=Path.cwd())
    wt = _worktree(session)
    name = activate._workspace_window_name("proj", wt)
    activate.open_or_select_workspace(project, wt, Config(projects=[], claude_cmd=INERT))
    assert _active_window_name(sock, session) == name

    _q(sock, "select-window", "-t", f"{session}:menu")
    assert _active_window_name(sock, session) == "menu"


def test_close_kills_workspace_menu_survives(mux):
    sock, session = mux
    tmux.bootstrap(menu_cmd=INERT, attach=False)

    project = Project(name="proj", root=Path.cwd())
    wt = _worktree(session)
    name = activate._workspace_window_name("proj", wt)
    activate.open_or_select_workspace(project, wt, Config(projects=[], claude_cmd=INERT))
    assert name in _window_names(sock, session)

    activate.close_workspace(wt)

    names = _window_names(sock, session)
    assert name not in names  # workspace window killed
    assert "menu" in names                    # menu (window 0) survives
    assert wt.lifecycle is Lifecycle.DORMANT
