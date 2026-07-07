"""tmux interaction via libtmux (pane listing, capture, layout, navigation).

claude-mux owns ONE dedicated tmux session, ``claude-mux`` (see ADR-0005). Its
window 0 is the ``menu``: the Textual Project→Worktree tree on the left with a
plain shell (rooted at the launch directory, ``./``) split off to its right. Each
entered Worktree is a full-screen window in that same session carrying the three-pane
Workspace layout: ``claude`` in a left vertical split (~50%), ``yazi`` top-right,
and a plain shell bottom-right. Navigation is ``select-window`` within the owned
session — there is no ``switch-client`` to external/per-project sessions.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import libtmux
from libtmux.constants import PaneDirection

from claude_mux.layouts import LayoutPlan


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


_DIRECTIONS = {"right": PaneDirection.Right, "below": PaneDirection.Below}

# The shell used to run pane commands, honouring $SHELL and falling back to sh.
_SHELL = "${SHELL:-/bin/sh}"


def launch_command_string(command: str) -> str:
    """Wrap ``command`` for ``respawn-pane`` so it runs in an *interactive* shell.

    Pure/string-only (unit-tested). We run the command as ``$SHELL -i -c '<cmd>;
    exec $SHELL'`` rather than handing it straight to tmux because tmux would run
    it *non-interactively* — never sourcing ``~/.zshrc`` / ``~/.bashrc``. An
    interactive shell resolves aliases, shell functions and rc-defined PATH (e.g.
    an nvm-installed ``claude`` or a ``george``-style alias), matching the old
    ``send_keys``-into-the-shell behaviour. The trailing ``exec $SHELL`` leaves an
    interactive shell in the pane after the command exits. ``$SHELL`` is expanded
    by the interactive shell (single-quoted here), not the outer shell.
    """
    inner = f"{command}; exec {_SHELL}"
    return f"exec {_SHELL} -i -c {shlex.quote(inner)}"


def _cmd_failed(result: object) -> bool:
    """True if a libtmux ``server.cmd`` result reports a tmux-level failure.

    ``server.cmd`` does not raise on a non-zero tmux exit (only when the tmux
    binary is missing) — it returns a result carrying ``returncode``/``stderr``.
    So a real failure has to be read off the result, not caught as an exception.
    """
    if result is None:
        return True
    rc = getattr(result, "returncode", 0)
    if rc not in (0, None):
        return True
    return bool(getattr(result, "stderr", None))


# How long to let the terminal quiesce before respawning the claude pane. After
# the layout's splits, sibling launches, ``select-window`` (full-screen resize)
# and ``select-pane`` are all done, this brief settle lets any query/reply bytes
# those operations provoked finish arriving in the pane's pty — so the following
# ``respawn-pane -k`` can discard them before claude starts. See ``launch_in_pane``.
CLAUDE_LAUNCH_SETTLE = 0.15


def _launch(server: libtmux.Server, pane_id: str, command: str) -> None:
    """Launch ``command`` in a pane by execing it, not by typing into a shell.

    ``respawn-pane -k`` replaces the pane's transient shell with the wrapped
    command (see ``launch_command_string``). The ``-k`` kill also discards
    whatever is buffered in the pane's pty, which is load-bearing for the claude
    pane: creating the splits and selecting/resizing the window makes tmux emit
    terminal queries whose ``\\x1b[0n`` (device-status-report) replies land in the
    pane. Launching claude *last* — after all that churn (see
    ``build_workspace_layout``'s ``launch_first`` and ``launch_in_pane``) — means
    ``-k`` flushes those stray replies right before claude starts, so they cannot
    be read as pre-typed input (``0n0n``). Claude still runs its own startup
    capability handshake (anthropics/claude-code#17787); that is claude-internal,
    but it now happens in a quiet, stable, freshly-flushed pty.
    Falls back to ``send_keys`` only if the respawn actually failed.
    """
    wrapped = launch_command_string(command)
    result: object = None
    try:
        result = server.cmd("respawn-pane", "-k", "-t", pane_id, wrapped)
    except Exception:
        result = None
    if _cmd_failed(result):
        # Respawn failed at the tmux level (or tmux was absent): type it in. May
        # show transient query bytes but still launches the command.
        pane = server.panes.get(pane_id=pane_id, default=None)
        if pane is not None:
            try:
                pane.send_keys(command, enter=True)
            except Exception:
                pass


def launch_in_pane(pane_id: str, command: str, settle: float = 0.0) -> None:
    """Launch ``command`` in an existing pane, optionally after a settle delay.

    Public entry for launching the claude pane *after* the window is full-screen
    and the pane has reached its final geometry (see ``build_workspace_layout``
    with ``launch_first=False``). ``settle`` sleeps first so the terminal's replies
    to the preceding split/select/resize churn finish arriving in the pty; the
    ``respawn-pane -k`` inside ``_launch`` then discards them before claude runs.
    A falsy ``command`` is a no-op (the pane stays a plain shell).
    """
    if not command:
        return
    if settle and settle > 0:
        time.sleep(settle)
    _launch(_server(), pane_id, command)


def build_workspace_layout(
    session_name: str, window_target: str, cwd: Path, plan: LayoutPlan, launch_first: bool = True
) -> dict:
    """Build ``plan``'s split layout in a window; return pane ids keyed by role.

    ``plan`` is a ``layouts.LayoutPlan``: ``plan.panes[0]`` is the window's initial
    pane and each later pane splits off an earlier one. Commands are launched via
    ``_launch`` (exec, not send-keys) so no terminal-query bytes leak into a pane;
    a pane whose spec has no command is left as a plain interactive shell. The
    first pane (claude) is left focused.

    ``launch_first=False`` builds every pane and launches every *non-first*
    command, but leaves the first pane's command unlaunched so the caller can
    start it last — after ``select-window``/``select-pane`` — via ``launch_in_pane``.
    This is how the claude pane avoids reading the split/select/resize churn's
    ``\\x1b[0n`` replies as pre-typed input (``0n0n``); see ``_launch``.
    """
    server = _server()
    window = _get_window(server, window_target)
    if window is None:
        raise RuntimeError(f"tmux window not found: {window_target!r}")

    cwd_str = str(cwd)

    first = plan.panes[0]
    panes = {first.role: (window.active_pane or window.panes[0])}

    # Create every split first, so all panes exist before any command is launched.
    for spec in plan.panes[1:]:
        # Non-initial panes always name a source; default to the first pane if not.
        source = panes[spec.frm or first.role]
        direction = _DIRECTIONS.get(spec.direction or "right", PaneDirection.Right)
        panes[spec.role] = source.split(
            direction=direction,
            start_directory=cwd_str,
            percentage=spec.percentage if spec.percentage is not None else 50,
            attach=False,
        )

    # Now launch the commands (execing each), leaving command-less panes as shells.
    # When ``launch_first`` is False the first pane (claude) is skipped here so the
    # caller can launch it last, once the window is full-screen and stable.
    for spec in plan.panes:
        if spec is first and not launch_first:
            continue
        if spec.command:
            _launch(server, panes[spec.role].pane_id, spec.command)

    # Leave the first (claude) pane focused within this window.
    try:
        panes[first.role].select()
    except Exception:
        pass

    return {role: pane.pane_id for role, pane in panes.items()}


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


# Width (percent of the menu window) given to the terminal that sits to the
# right of the Textual tree. The tree keeps the majority so its rows stay legible.
MENU_TERMINAL_PERCENT = 40


def ensure_menu_session(menu_cmd: str | None = None, terminal_cwd: Path | None = None) -> None:
    """Ensure the owned session exists with a ``menu`` window (idempotent).

    Guard from the ADR contract: create the session when absent; if it exists but
    has no ``menu`` window (stale/foreign), add one. Never attaches or switches a
    client — placement is ``bootstrap``'s job. ``menu_cmd`` runs AS the menu
    window's process (if it exits the window closes), so callers pass a foreground
    command; defaults to the in-place Textual menu.

    The menu window carries two panes: the Textual tree on the left and a plain
    interactive shell on the right (rooted at ``terminal_cwd`` — the directory
    ``claude-mux`` was launched from, i.e. ``./``). ``ensure_menu_terminal`` adds
    that shell idempotently, so this stays safe to call on every workspace open.
    """
    if menu_cmd is None:
        menu_cmd = _menu_command()
    server = _server()
    if not (server.is_alive() and server.has_session(MUX_SESSION)):
        server.cmd("new-session", "-d", "-s", MUX_SESSION, "-n", "menu", menu_cmd)
        _style_status_bar(server)
        ensure_menu_terminal(terminal_cwd)
        return
    if find_window(MUX_SESSION, "menu") is None:
        server.cmd("new-window", "-d", "-t", MUX_SESSION, "-n", "menu", menu_cmd)
    _style_status_bar(server)
    ensure_menu_terminal(terminal_cwd)


# Quiet, informative status bar (claude-mux TUI.dc.html, screen 1g): the loud
# full-width green tmux default becomes a dark hairline with one green accent.
# Every option is scoped to ``-t MUX_SESSION`` so the operator's own tmux config
# and other sessions are never touched.
_STATUS_OPTIONS: list[tuple[str, str]] = [
    ("status-style", "bg=#141618,fg=#8b9298"),
    ("status-left", "#[fg=#6cbf3f,bold] claude-mux #[default]"),
    ("status-left-length", "24"),
    ("status-right", "#[fg=#9aa1a8]#h #[fg=#4a5157]· #[fg=#8b9298]%H:%M "),
    ("status-right-length", "40"),
    ("window-status-format", " #I:#W "),
    ("window-status-style", "fg=#8b9298"),
    ("window-status-current-format", " #I:#W "),
    ("window-status-current-style", "fg=#6cbf3f,bg=#1b1d1f,bold"),
    ("message-style", "bg=#3C8321,fg=#ffffff"),
]


def _style_status_bar(server: "libtmux.Server") -> None:
    """Apply the quiet dark status bar to the owned session (best effort).

    Session-scoped and idempotent — safe to re-run on every workspace open. Option
    names are stable across modern tmux, but any failure is swallowed so styling
    can never block session bootstrap.
    """
    for option, value in _STATUS_OPTIONS:
        try:
            server.cmd("set-option", "-t", MUX_SESSION, option, value)
        except Exception:
            pass


def ensure_menu_terminal(cwd: Path | None = None, percentage: int = MENU_TERMINAL_PERCENT) -> None:
    """Split the menu window so a plain shell sits to the right of the tree.

    Idempotent: only splits while the menu window still has its single (Textual)
    pane, so repeated calls — ``ensure_menu_session`` runs on every workspace
    open — never stack up extra terminals. The new right-hand pane is left as a
    plain interactive shell (no command) rooted at ``cwd`` (defaults to the
    launch directory, ``./``). Focus is returned to the tree pane so the operator
    still drives the menu by default; the terminal is one ``select-pane`` away.
    """
    server = _server()
    if not server.is_alive():
        return
    window_id = find_window(MUX_SESSION, "menu")
    if window_id is None:
        return
    window = _get_window(server, window_id)
    if window is None or len(window.panes) > 1:
        return  # no menu window, or the terminal is already present
    menu_pane = window.active_pane or window.panes[0]
    try:
        menu_pane.split(
            direction=PaneDirection.Right,
            start_directory=str(cwd or Path.cwd()),
            percentage=percentage,
            attach=False,
        )
    except Exception:
        return
    # Keep the Textual tree focused; the operator opts into the terminal explicitly.
    try:
        menu_pane.select()
    except Exception:
        pass


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
