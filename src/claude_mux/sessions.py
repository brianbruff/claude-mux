"""Read-only access to ~/.claude session index data.

This module never writes to ~/.claude. It decodes each project's
``sessions-index.json`` (the Session Index) into :class:`SessionMeta`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from claude_mux.model import SessionMeta

# Any character that is not ASCII alphanumeric is replaced by '-' by Claude
# Code when it encodes a project path into a slug directory name.
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")


def claude_projects_dir() -> Path:
    """Return the Claude projects directory (~/.claude/projects)."""
    return Path.home() / ".claude" / "projects"


def encode_slug(path: Path) -> str:
    """Encode a filesystem path into a Claude project slug.

    Claude Code replaces ``/`` and every other non-alphanumeric character with
    ``-`` (e.g. ``/A/b c`` -> ``-A-b-c``; ``/x/brian.keating`` -> ``-x-brian-keating``).
    """
    return _NON_ALNUM.sub("-", str(path))


def _to_epoch(value: object) -> float:
    """Best-effort convert an ISO-8601 string to epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    text = value.strip()
    # Python's fromisoformat handles a trailing 'Z' from 3.11 on, but normalise
    # defensively so the conversion is robust.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def read_session_index(slug: str) -> list[SessionMeta]:
    """Parse a slug's sessions-index.json; return [] if absent or unreadable."""
    index_path = claude_projects_dir() / slug / "sessions-index.json"
    if not index_path.exists():
        return []

    try:
        with index_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []

    entries = data.get("entries", []) if isinstance(data, dict) else []
    results: list[SessionMeta] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            results.append(
                SessionMeta(
                    session_id=str(entry.get("sessionId", "")),
                    summary=str(entry.get("summary", "")),
                    first_prompt=str(entry.get("firstPrompt", "")),
                    message_count=int(entry.get("messageCount", 0) or 0),
                    modified=_to_epoch(entry.get("modified")),
                    git_branch=str(entry.get("gitBranch", "")),
                    project_path=Path(str(entry.get("projectPath", ""))),
                    jsonl_path=Path(str(entry.get("fullPath", ""))),
                )
            )
        except (TypeError, ValueError):
            continue
    return results


def latest_session(slug: str) -> SessionMeta | None:
    """Return the newest SessionMeta by modified time, or None."""
    entries = read_session_index(slug)
    if not entries:
        return None
    return max(entries, key=lambda s: s.modified)
