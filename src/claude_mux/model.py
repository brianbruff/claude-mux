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


class AgentKind(str, Enum):
    """Which AI coding agent is running in a pane.

    ``claude`` is the fully-supported agent (session index + hooks + scrape). The
    others are detected by process name and shown scrape-only — no conversation
    summary, no ``--resume``. ``UNKNOWN`` is reserved; ``tmux.classify_agent``
    returns ``None`` (not ``UNKNOWN``) for a pane that is not a known agent.
    """

    CLAUDE = "claude"
    GEMINI = "gemini"
    CODEX = "codex"
    COPILOT = "copilot"
    OPENCODE = "opencode"
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
class LiveAgent:
    """A tmux pane running an AI coding agent, enriched with session + scrape data.

    ``kind`` distinguishes claude (full metadata) from the detected-only agents
    (gemini/codex/copilot/opencode), which carry ``kind`` + scrape extras but no
    ``session_id``/``summary``/``idle_seconds``.
    """

    pane_id: str          # tmux unique pane id, e.g. '%12'
    session_name: str     # tmux session name
    window_index: int
    pid: int
    cwd: Path
    kind: "AgentKind" = AgentKind.CLAUDE
    session_id: Optional[str] = None  # from Pane Map (authoritative) or heuristic
    activity: Activity = Activity.UNKNOWN
    summary: Optional[str] = None     # from SessionMeta
    model: Optional[str] = None       # scrape extra
    context_pct: Optional[int] = None  # scrape extra
    cost_usd: Optional[float] = None   # scrape extra
    elapsed: Optional[str] = None      # scrape extra, raw e.g. "2m 59s"
    idle_seconds: Optional[int] = None


# Back-compat alias: the type was single-agent (claude-only) before multi-agent
# support. Existing imports/constructions of ``LiveClaude`` keep working.
LiveClaude = LiveAgent


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
    # ``live`` is the PRIMARY agent (claude if present, else the first agent) and
    # backs the single-line row + status counts. ``agents`` is every agent pane in
    # this worktree and backs the detail panel; ``live`` is one of ``agents``.
    live: Optional[LiveAgent] = None
    agents: list["LiveAgent"] = field(default_factory=list)
    latest_session: Optional[SessionMeta] = None
