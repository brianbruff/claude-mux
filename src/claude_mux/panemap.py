"""Append-only state files (pane map + events) under ~/.claude/claude-mux/.

The pane map is the authoritative ``tmux_pane -> sessionId`` mapping (ADR-0004);
the events file is the near-instant Status transition source. Both are JSONL
(one JSON object per line) so writes are cheap, append-only, and crash-safe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PaneMapEntry:
    """A mapping from a tmux pane to a Claude session at a point in time."""

    tmux_pane: str
    session_id: str
    cwd: Path
    ts: float


@dataclass
class StatusEvent:
    """A lifecycle/activity event emitted by a Claude hook."""

    tmux_pane: str
    session_id: str | None
    kind: str
    ts: float


def state_dir() -> Path:
    """Return the state directory (~/.claude/claude-mux), creating it if needed."""
    path = Path.home() / ".claude" / "claude-mux"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pane_map_path() -> Path:
    """Return the pane map file path (panes.jsonl)."""
    return state_dir() / "panes.jsonl"


def events_path() -> Path:
    """Return the events file path (events.jsonl)."""
    return state_dir() / "events.jsonl"


def _read_lines(path: Path) -> list[dict]:
    """Read a JSONL file into a list of dicts, skipping blank/malformed lines."""
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def _append_line(path: Path, obj: dict) -> None:
    """Append one JSON object as a line to a JSONL file (creating parents)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def read_pane_map() -> dict[str, PaneMapEntry]:
    """Read the pane map keyed by tmux_pane (last write wins)."""
    result: dict[str, PaneMapEntry] = {}
    for obj in _read_lines(pane_map_path()):
        try:
            pane = obj["tmux_pane"]
            entry = PaneMapEntry(
                tmux_pane=pane,
                session_id=obj["session_id"],
                cwd=Path(obj["cwd"]),
                ts=float(obj["ts"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        result[pane] = entry  # last write wins
    return result


def read_events(since_ts: float = 0.0) -> list[StatusEvent]:
    """Read status events with ``ts >= since_ts`` (default 0.0 -> all), in file order."""
    events: list[StatusEvent] = []
    for obj in _read_lines(events_path()):
        try:
            ts = float(obj["ts"])
            ev = StatusEvent(
                tmux_pane=obj["tmux_pane"],
                session_id=obj.get("session_id"),
                kind=obj["kind"],
                ts=ts,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if ts >= since_ts:
            events.append(ev)
    return events


def append_pane_map(entry: PaneMapEntry) -> None:
    """Append a PaneMapEntry to the pane map file."""
    _append_line(
        pane_map_path(),
        {
            "tmux_pane": entry.tmux_pane,
            "session_id": entry.session_id,
            "cwd": str(entry.cwd),
            "ts": entry.ts,
        },
    )


def append_event(ev: StatusEvent) -> None:
    """Append a StatusEvent to the events file."""
    _append_line(
        events_path(),
        {
            "tmux_pane": ev.tmux_pane,
            "session_id": ev.session_id,
            "kind": ev.kind,
            "ts": ev.ts,
        },
    )
