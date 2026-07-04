"""Persisted Menu tree state (which Projects are collapsed).

The Menu is a two-level Project -> Worktree tree; only Project rows collapse.
This module persists the *set of collapsed Project roots* to a small JSON file
so the operator's expand/collapse layout survives across sessions. It is kept
separate from ``config.toml`` on purpose: config.toml is the hand-editable list
of Projects (CONTEXT.md), whereas this is throwaway view state claude-mux owns
outright and rewrites freely.

State is keyed by a Project's absolute ``root`` path. An unknown key (a Project
that was removed, or one never seen) is simply ignored on load, and a Project
absent from the file defaults to expanded.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def default_state_path() -> Path:
    """Return the default tree-state path (~/.config/claude-mux/tree-state.json).

    The ``CLAUDE_MUX_STATE_DIR`` environment variable overrides the parent
    directory (used by the test suite to keep view state out of the real home).
    """
    override = os.environ.get("CLAUDE_MUX_STATE_DIR")
    base = Path(override) if override else Path.home() / ".config" / "claude-mux"
    return base / "tree-state.json"


def load_collapsed(path: Path | None = None) -> set[Path]:
    """Load the set of collapsed Project roots; missing/invalid file -> empty set.

    Reads are best-effort: a malformed or unreadable file yields an empty set
    (everything expanded) rather than an error, since this is disposable view
    state and must never block the Menu from opening.
    """
    state_path = path if path is not None else default_state_path()
    if not state_path.exists():
        return set()
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        collapsed = data.get("collapsed_projects", [])
        return {Path(str(p)) for p in collapsed}
    except (OSError, ValueError, TypeError):
        return set()


def save_collapsed(collapsed: set[Path], path: Path | None = None) -> None:
    """Write the set of collapsed Project roots atomically.

    The parent directory is created if needed. The write is atomic (temp file in
    the same dir + ``os.replace``). Paths are serialized sorted so the file is
    stable across writes (no spurious diffs) and human-inspectable.
    """
    state_path = path if path is not None else default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    data = {"collapsed_projects": sorted(str(p) for p in collapsed)}
    contents = json.dumps(data, indent=2).encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(
        dir=str(state_path.parent), prefix=".tree-state-", suffix=".json.tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, state_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
