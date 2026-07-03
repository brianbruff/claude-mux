"""Claude Code hook installation and the hook entrypoint (~/.claude/settings.json).

Install (``install_hooks``) MERGES claude-mux hook entries into the user's
settings without clobbering their existing hooks, and is idempotent. The hook
entrypoint (``run_hook``) is invoked by Claude Code as a subprocess on every
SessionStart/SessionEnd/Notification/Stop; it MUST NEVER raise or exit non-zero,
since a failing hook can disrupt a running claude (ADR-0004).
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

from claude_mux.panemap import (
    PaneMapEntry,
    StatusEvent,
    append_event,
    append_pane_map,
)

# Claude Code hook events claude-mux registers, and the StatusEvent.kind each maps to.
EVENT_KINDS: dict[str, str] = {
    "SessionStart": "start",
    "SessionEnd": "end",
    "Notification": "notification",
    "Stop": "stop",
}

# Substring that identifies a claude-mux hook command regardless of interpreter path.
_MARKER = "claude_mux hook"


def settings_path() -> Path:
    """Return the Claude settings path (~/.claude/settings.json)."""
    return Path.home() / ".claude" / "settings.json"


def hook_command() -> str:
    """Return the base command Claude invokes for a hook event (event appended per-event).

    Uses the current interpreter so the hook resolves the same environment that
    installed it, e.g. ``/path/to/python -m claude_mux hook``.
    """
    return f"{shlex.quote(sys.executable)} -m claude_mux hook"


def _event_command(event: str) -> str:
    """Full command string for a given hook event."""
    return f"{hook_command()} {event}"


def _event_marker(event: str) -> str:
    """Substring identifying our command for a specific event."""
    return f"{_MARKER} {event}"


def _load_settings(path: Path) -> dict:
    """Load settings.json into a dict; missing/empty file -> {}."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_settings(path: Path, data: dict) -> None:
    """Write settings.json (pretty-printed, trailing newline), creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _iter_group_commands(group: dict):
    """Yield (index, command) for each command hook in a hook group's 'hooks' list."""
    inner = group.get("hooks")
    if not isinstance(inner, list):
        return
    for idx, hook in enumerate(inner):
        if isinstance(hook, dict) and isinstance(hook.get("command"), str):
            yield idx, hook["command"]


def install_hooks(settings: Path | None = None) -> list[str]:
    """Merge claude-mux hook entries into settings idempotently; return changes made.

    Never clobbers existing user hooks: for each event we only append a new hook
    group when no claude-mux command for that event is already present.
    """
    path = settings or settings_path()
    data = _load_settings(path)

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"'hooks' in {path} is not a JSON object")

    changes: list[str] = []
    for event in EVENT_KINDS:
        event_list = hooks.setdefault(event, [])
        if not isinstance(event_list, list):
            raise ValueError(f"hooks.{event} in {path} is not a list")

        marker = _event_marker(event)
        already = any(
            marker in cmd
            for group in event_list
            if isinstance(group, dict)
            for _, cmd in _iter_group_commands(group)
        )
        if already:
            continue

        event_list.append(
            {"hooks": [{"type": "command", "command": _event_command(event)}]}
        )
        changes.append(f"added {event} hook")

    if changes:
        _write_settings(path, data)
    return changes


def uninstall_hooks(settings: Path | None = None) -> list[str]:
    """Remove claude-mux hook entries from settings; return changes made.

    Only removes hooks whose command is a claude-mux command; preserves every
    other hook and hook group the user has configured.
    """
    path = settings or settings_path()
    if not path.exists():
        return []
    data = _load_settings(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []

    changes: list[str] = []
    for event in list(hooks.keys()):
        event_list = hooks.get(event)
        if not isinstance(event_list, list):
            continue

        new_groups: list = []
        removed_here = False
        for group in event_list:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                new_groups.append(group)
                continue
            kept = [
                hook
                for hook in inner
                if not (
                    isinstance(hook, dict)
                    and isinstance(hook.get("command"), str)
                    and _MARKER in hook["command"]
                )
            ]
            if len(kept) != len(inner):
                removed_here = True
            if kept:
                group["hooks"] = kept
                new_groups.append(group)
            # else: drop a group that held only claude-mux hooks

        if removed_here:
            changes.append(f"removed {event} hook")
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]

    if changes:
        _write_settings(path, data)
    return changes


def run_hook(event_name: str) -> None:
    """Hook entrypoint: read hook JSON from stdin and append pane map/events.

    NEVER crashes claude: all errors are swallowed and the process exits cleanly.
    """
    try:
        _run_hook_inner(event_name)
    except BaseException:  # noqa: BLE001 - a hook must never break a running claude
        pass


def _run_hook_inner(event_name: str) -> None:
    """Body of run_hook; every step is individually guarded."""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    data: dict = {}
    if raw and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}

    tmux_pane = os.environ.get("TMUX_PANE")
    if not tmux_pane:
        # No pane to correlate against; nothing useful to record.
        return

    session_id = data.get("session_id")
    ts = time.time()
    kind = EVENT_KINDS.get(event_name, event_name.lower())

    if event_name == "SessionStart" and session_id:
        cwd_val = data.get("cwd") or os.getcwd()
        try:
            append_pane_map(
                PaneMapEntry(
                    tmux_pane=tmux_pane,
                    session_id=session_id,
                    cwd=Path(str(cwd_val)),
                    ts=ts,
                )
            )
        except Exception:
            pass

    try:
        append_event(
            StatusEvent(
                tmux_pane=tmux_pane,
                session_id=session_id,
                kind=kind,
                ts=ts,
            )
        )
    except Exception:
        pass
