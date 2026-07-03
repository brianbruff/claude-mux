"""tmux interaction via libtmux (pane listing, capture, layout, navigation).

One tmux session per Project, one window (Workspace) per Worktree (see ADR-0002).
A Workspace window has three panes: ``claude`` in a left vertical split (~50%),
``yazi`` top-right, and a plain shell bottom-right.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import libtmux
from libtmux.constants import PaneDirection


@dataclass
class PaneInfo:
    """Metadata for a single tmux pane."""

    pane_id: str
    session_name: str
    window_index: int
    pane_index: int
    current_command: str
    pid: int
    current_path: Path


# ``claude`` reports its foreground process command variously across versions:
# the literal ``claude``, the underlying ``node`` runtime, or (in some builds)
# a bare semver-shaped version string such as ``2.1.199``.
_SEMVER_RE = re.compile(r"^v?\d+\.\d+(?:\.\d+)*$")


def _server() -> libtmux.Server:
    """Return a handle to the default tmux server (does not start it)."""
    return libtmux.Server()


def _get_session(server: libtmux.Server, session_name: str) -> Optional[libtmux.Session]:
    """Look up a session by name; return None if the server/session is absent."""
    if not server.is_alive():
        return None
    try:
        return server.sessions.get(session_name=session_name, default=None)
    except Exception:
        for sess in server.sessions:
            if sess.session_name == session_name:
                return sess
        return None


def _get_window(server: libtmux.Server, window_target: str) -> Optional[libtmux.Window]:
    """Look up a window by its window id (e.g. '@5'); None if absent."""
    if not server.is_alive():
        return None
    for window in server.windows:
        if window.window_id == window_target:
            return window
    return None


def is_claude_command(cmd: str) -> bool:
    """Return True if a pane command looks like a running claude ('claude', 'node', or semver)."""
    if not cmd:
        return False
    stripped = cmd.strip()
    lowered = stripped.lower()
    if lowered in ("claude", "node"):
        return True
    return bool(_SEMVER_RE.match(stripped))


def list_panes() -> list[PaneInfo]:
    """List all panes across all tmux sessions. Empty if no server is running."""
    server = _server()
    if not server.is_alive():
        return []

    panes: list[PaneInfo] = []
    try:
        raw_panes = list(server.panes)
    except Exception:
        return []

    for pane in raw_panes:
        try:
            path_str = pane.pane_current_path
            info = PaneInfo(
                pane_id=pane.pane_id or "",
                session_name=pane.session_name or "",
                window_index=_to_int(pane.window_index),
                pane_index=_to_int(pane.pane_index),
                current_command=pane.pane_current_command or "",
                pid=_to_int(pane.pane_pid),
                current_path=Path(path_str) if path_str else Path("."),
            )
        except Exception:
            continue
        panes.append(info)
    return panes


def _to_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def capture_pane(pane_id: str, lines: int = 8) -> str:
    """Capture the last N lines of a pane's visible content. Empty string on any failure."""
    server = _server()
    if not server.is_alive():
        return ""
    try:
        pane = server.panes.get(pane_id=pane_id, default=None)
        if pane is None:
            pane = libtmux.Pane.from_pane_id(server=server, pane_id=pane_id)
        if pane is None:
            return ""
        captured = pane.capture_pane()
    except Exception:
        return ""

    if captured is None:
        return ""
    if isinstance(captured, str):
        rows = captured.splitlines()
    else:
        rows = list(captured)
    if lines is not None and lines > 0:
        rows = rows[-lines:]
    return "\n".join(rows)


def ensure_session(session_name: str, cwd: Path) -> None:
    """Create the tmux session detached if it does not already exist (idempotent)."""
    server = _server()
    if server.is_alive() and server.has_session(session_name):
        return
    server.new_session(
        session_name=session_name,
        start_directory=str(cwd),
        attach=False,
    )


def new_window(session_name: str, window_name: str, cwd: Path) -> str:
    """Create a new window in the Project's session and return its window id target."""
    server = _server()
    session = _get_session(server, session_name)
    if session is None:
        raise RuntimeError(f"tmux session not found: {session_name!r}")
    window = session.new_window(
        window_name=window_name,
        start_directory=str(cwd),
        attach=False,
    )
    return window.window_id


def build_workspace_layout(session_name: str, window_target: str, cwd: Path, claude_cmd: str) -> dict:
    """Build the claude/yazi/shell split layout; return pane ids keyed by role.

    Layout: ``claude`` in a left vertical split (~50%), ``yazi`` top-right, a plain
    shell bottom-right. ``claude_cmd`` is launched in the left pane and ``yazi`` in
    the top-right pane; the bottom-right pane is left as a plain interactive shell.
    """
    server = _server()
    window = _get_window(server, window_target)
    if window is None:
        raise RuntimeError(f"tmux window not found: {window_target!r}")

    cwd_str = str(cwd)

    # The window's initial pane becomes the left-hand claude pane.
    claude_pane = window.active_pane or window.panes[0]

    # Split off the right half -> top-right (yazi) at ~50% width.
    yazi_pane = claude_pane.split(
        direction=PaneDirection.Right,
        start_directory=cwd_str,
        percentage=50,
        attach=False,
    )

    # Split the right pane vertically -> bottom-right (shell) at ~50% height.
    shell_pane = yazi_pane.split(
        direction=PaneDirection.Below,
        start_directory=cwd_str,
        percentage=50,
        attach=False,
    )

    # Launch commands. send_keys leaves an interactive shell if the command exits.
    if claude_cmd:
        claude_pane.send_keys(claude_cmd, enter=True)
    yazi_pane.send_keys("yazi", enter=True)

    # Leave the claude pane focused within this window.
    try:
        claude_pane.select()
    except Exception:
        pass

    return {
        "claude": claude_pane.pane_id,
        "yazi": yazi_pane.pane_id,
        "shell": shell_pane.pane_id,
    }


def jump_to(session_name: str, window_target: str | None = None, pane_id: str | None = None) -> None:
    """Switch client to a session/window/pane (works from inside a display-popup).

    Uses raw tmux commands so the caller's attached client (including the client
    behind a ``display-popup``) is the one that gets switched.
    """
    server = _server()
    if not server.is_alive():
        return
    server.cmd("switch-client", "-t", session_name)
    if window_target is not None:
        server.cmd("select-window", "-t", window_target)
    if pane_id is not None:
        server.cmd("select-pane", "-t", pane_id)


def kill_window(session_name: str, window_target: str) -> None:
    """Kill a tmux window (the Workspace teardown primitive; never touches git).

    ``window_target`` is normally a bare window NAME (the sanitized branch). tmux
    resolves an unqualified name globally (most-recently-used match), and libtmux's
    ``Session.kill_window`` forwards a bare ``-t <name>`` without a ``session:``
    prefix — so a same-named window in another Project's session could be killed.
    Session-qualify the target (``session:name``) so the kill is scoped correctly.
    A ``@``-prefixed window id is already server-global unique and is passed as-is.
    """
    server = _server()
    session = _get_session(server, session_name)
    if session is None:
        return
    if window_target.startswith("@") or ":" in window_target:
        target = window_target
    else:
        target = f"{session_name}:{window_target}"
    try:
        session.kill_window(target)
    except Exception:
        # Window may already be gone; killing is idempotent from the caller's view.
        pass
