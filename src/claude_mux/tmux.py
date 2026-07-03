"""tmux interaction via libtmux (pane listing, capture, layout, navigation).

claude-mux owns ONE dedicated tmux session, ``claude-mux`` (see ADR-0005). Its
window 0 is the ``menu`` (the Textual Project→Worktree tree); each entered
Worktree is a full-screen window in that same session carrying the three-pane
Workspace layout: ``claude`` in a left vertical split (~50%), ``yazi`` top-right,
and a plain shell bottom-right. Navigation is ``select-window`` within the owned
session — there is no ``switch-client`` to external/per-project sessions.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import libtmux
from libtmux.constants import PaneDirection


# The single dedicated tmux session claude-mux owns (ADR-0005). Module-level so
# tests can monkeypatch it to a throwaway name before touching a live server.
MUX_SESSION = "claude-mux"

# Optional private socket name (``tmux -L <name>`` / ``libtmux.Server(socket_name=)``).
# None => the operator's default server. Tests inject a private socket here so
# live-tmux work never touches the operator's real default server.
_SOCKET_NAME: Optional[str] = None


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
    """Return a handle to the tmux server (does not start it).

    Honors ``_SOCKET_NAME`` so a private socket can be injected for tests
    (``libtmux.Server(socket_name=...)``); falls back to the default server.
    """
    if _SOCKET_NAME:
        return libtmux.Server(socket_name=_SOCKET_NAME)
    return libtmux.Server()


def in_tmux() -> bool:
    """True when running inside a tmux client (``$TMUX`` is set)."""
    return bool(os.environ.get("TMUX"))


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


def find_window(session_name: str, window_name: str) -> str | None:
    """Return the window id of an EXACT-named window in a session, else None.

    tmux resolves bare names with fnmatch/prefix matching, so a Workspace name
    like ``proj/feature/foo`` could match the wrong window. Compare names
    exactly and return the unambiguous ``@``-prefixed window id for later
    targeting.
    """
    server = _server()
    if not server.is_alive():
        return None
    try:
        out = server.cmd(
            "list-windows", "-t", session_name,
            "-F", "#{window_id}\t#{window_name}",
        ).stdout
    except Exception:
        return None
    for line in out:
        wid, _, name = line.partition("\t")
        if name == window_name:
            return wid
    return None


def _menu_command() -> str:
    """Shell command run as window 0's process: the in-place Textual menu.

    Uses ``_menu`` (not ``dashboard``) so it does NOT re-bootstrap/recurse. Built
    from ``sys.executable`` (mirrors hooks.hook_command) so it works under uv/venv.
    """
    return f"{shlex.quote(sys.executable)} -m claude_mux _menu"


def ensure_menu_session(menu_cmd: str | None = None) -> None:
    """Ensure the owned session exists with a ``menu`` window (idempotent).

    Guard from the ADR contract: create the session when absent; if it exists but
    has no ``menu`` window (stale/foreign), add one. Never attaches or switches a
    client — placement is ``bootstrap``'s job. ``menu_cmd`` runs AS the menu
    window's process (if it exits the window closes), so callers pass a foreground
    command; defaults to the in-place Textual menu.
    """
    if menu_cmd is None:
        menu_cmd = _menu_command()
    server = _server()
    if not (server.is_alive() and server.has_session(MUX_SESSION)):
        server.cmd("new-session", "-d", "-s", MUX_SESSION, "-n", "menu", menu_cmd)
        return
    if find_window(MUX_SESSION, "menu") is None:
        server.cmd("new-window", "-d", "-t", MUX_SESSION, "-n", "menu", menu_cmd)


def install_menu_keybinding(session: str | None = None) -> None:
    """Bind ``prefix + m`` to jump to the menu window (ADR-0005).

    Prefix-gated so it never leaks into claude/vim typing. tmux key tables are
    server-GLOBAL (there is no per-session key table), so a bare ``bind-key``
    would clobber the built-in ``prefix m`` (mark-pane) for *every* session
    sharing the operator's server — including unrelated ones. To keep the effect
    scoped to the owned ``claude-mux`` session, the binding is guarded by an
    ``if-shell`` on the active session name: it jumps to the menu only while the
    key is pressed inside the owned session, and otherwise falls through to the
    built-in ``select-pane -m`` (mark-pane) so other sessions are untouched.
    """
    session = session or MUX_SESSION
    server = _server()
    if not server.is_alive():
        return
    try:
        server.cmd(
            "bind-key", "-T", "prefix", "m",
            "if-shell", "-F", f"#{{==:#{{session_name}},{session}}}",
            f"select-window -t {session}:menu",
            "select-pane -m",
        )
    except Exception:
        pass


def _attach_argv() -> list[str]:
    """argv for ``tmux [-L sock] attach-session -t <MUX_SESSION>`` (socket-aware)."""
    argv = ["tmux"]
    if _SOCKET_NAME:
        argv += ["-L", _SOCKET_NAME]
    argv += ["attach-session", "-t", MUX_SESSION]
    return argv


def bootstrap(menu_cmd: str | None = None, attach: bool = True) -> None:
    """Ensure the owned session + menu + keybinding, then place the operator in it.

    Placement (ADR-0005): inside tmux -> ``switch-client`` the caller's client to
    the owned session; outside tmux -> ``exec`` ``tmux attach-session`` so the
    operator's real TTY is inherited (a subprocess/daemon attach fails with
    'not a terminal'). ``attach=False`` builds the session without placing a
    client (used by tests and headless callers). Running while already inside the
    menu window is safe: the guard skips creation and switch-client is a no-op.
    """
    ensure_menu_session(menu_cmd)
    install_menu_keybinding()
    if not attach:
        return
    if in_tmux():
        server = _server()
        if not server.is_alive():
            return
        client = _current_client(server)
        if client:
            server.cmd("switch-client", "-c", client, "-t", MUX_SESSION)
        else:
            server.cmd("switch-client", "-t", MUX_SESSION)
    else:
        # Replace this process with the attaching tmux client so it inherits the
        # operator's controlling terminal. Requires a real TTY.
        os.execvp("tmux", _attach_argv())


def _current_client(server: libtmux.Server) -> str | None:
    """The tmux client displaying claude-mux (derived from ``$TMUX_PANE``), so
    switch-client targets the operator's terminal, not tmux's most-recently-
    active client. Works for both the dashboard window and a display-popup
    (the popup's ``-E`` command inherits ``$TMUX_PANE`` of the underlying pane).

    Returns None when the pane/client can't be resolved (e.g. not running inside
    tmux) so callers can fall back to the single-client ``switch-client`` form.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    try:
        sess = server.cmd("display-message", "-p", "-t", pane, "#{session_name}").stdout
        session_name = sess[0] if sess else None
        if not session_name:
            return None
        # Several clients can share one session (mirrored attach). The order of
        # ``list-clients`` is not the invoking client, so ``out[0]`` may switch
        # the wrong terminal. Prefer the most-recently-active client on the
        # session — the one whose keypress just opened the popup — by comparing
        # ``client_activity`` (a Unix timestamp). Falls back to first on ties.
        out = server.cmd(
            "list-clients", "-t", session_name,
            "-F", "#{client_activity} #{client_name}",
        ).stdout
        best_name: str | None = None
        best_activity = float("-inf")
        for line in out:
            if not line.strip():
                continue
            activity_str, _, name = line.partition(" ")
            if not name:
                continue
            try:
                activity = float(activity_str)
            except ValueError:
                activity = float("-inf")
            if activity > best_activity:
                best_activity = activity
                best_name = name
        return best_name
    except Exception:
        return None


def jump_to(session_name: str, window_target: str | None = None, pane_id: str | None = None) -> None:
    """Surface a window/pane within the owned session via ``select-window``.

    Intra-session navigation only (ADR-0005): the attached client already lives
    in ``claude-mux``, so ``select-window`` (+ ``select-pane``) is the full-screen
    swap — a tmux window inherently fills the whole client. No ``switch-client``:
    the one-time launch placement is handled by ``bootstrap``. ``session_name`` is
    retained for signature compatibility; targets are resolved against the passed
    (session-qualified) ``window_target`` / ``pane_id``.
    """
    server = _server()
    if not server.is_alive():
        return
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
