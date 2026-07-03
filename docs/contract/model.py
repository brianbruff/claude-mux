"""Core domain model for claude-mux. See CONTEXT.md for terminology.

This module is the shared contract every other module codes against. Keep it
free of I/O and side effects — it is pure data.

SCAFFOLD: copy this file verbatim to src/claude_mux/model.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class Lifecycle(str, Enum):
    """A Worktree is in exactly one of these states (see CONTEXT.md)."""

    DORMANT = "dormant"  # exists on disk (git worktree), no tmux window
    OPEN = "open"        # has a Workspace (window) but no running claude
    LIVE = "live"        # Workspace with a running Claude


class Activity(str, Enum):
    """Derived activity of a Live Claude."""

    RUNNING = "running"
    WAITING = "waiting"  # blocked on the operator (input or permission prompt)
    IDLE = "idle"
    UNKNOWN = "unknown"


@dataclass
class SessionMeta:
    """One entry decoded from a project slug's sessions-index.json."""

    session_id: str
    summary: str
    first_prompt: str
    message_count: int
    modified: float  # epoch seconds
    git_branch: str
    project_path: Path
    jsonl_path: Path


@dataclass
class LiveClaude:
    """A tmux pane running `claude`, enriched with session + scrape data."""

    pane_id: str          # tmux unique pane id, e.g. '%12'
    session_name: str     # tmux session name
    window_index: int
    pid: int
    cwd: Path
    session_id: Optional[str] = None  # from Pane Map (authoritative) or heuristic
    activity: Activity = Activity.UNKNOWN
    summary: Optional[str] = None     # from SessionMeta
    model: Optional[str] = None       # scrape extra
    context_pct: Optional[int] = None  # scrape extra
    cost_usd: Optional[float] = None   # scrape extra
    elapsed: Optional[str] = None      # scrape extra, raw e.g. "2m 59s"
    idle_seconds: Optional[int] = None


@dataclass
class Project:
    """A top-level git repository with zero or more Worktrees."""

    name: str
    root: Path
    session_name: str = ""  # tmux session name; defaults to name
    worktrees: list["Worktree"] = field(default_factory=list)


@dataclass
class Worktree:
    """A git worktree of a Project. Each is its own Claude Project Slug."""

    project_name: str
    path: Path
    branch: str
    is_primary: bool = False
    slug: str = ""  # claude Project Slug for this path
    lifecycle: Lifecycle = Lifecycle.DORMANT
    live: Optional[LiveClaude] = None
    latest_session: Optional[SessionMeta] = None
