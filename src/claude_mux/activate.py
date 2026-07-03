"""Lifecycle primitives: activate a Worktree into a running Workspace + Claude.

Activate is the shared lifecycle primitive (see CONTEXT.md / ADR-0002): it takes a
Dormant Worktree (on disk, no tmux window) to Live by building its Workspace — one
tmux window inside the Project's tmux session — and auto-launching ``claude`` in the
left pane, resuming the worktree's most recent Session when one exists.

Layering:
  * ``new_worktree`` = create the git worktree, then ``activate`` it.
  * ``close_workspace`` tears down only the tmux window (Live/Open -> Dormant); it
    NEVER touches git.
  * ``remove_worktree`` is the distinct, destructive git teardown; callers confirm.
"""
from __future__ import annotations

from claude_mux import git, sessions, tmux
from claude_mux.config import Config
from claude_mux.model import Lifecycle, Project, Worktree


def _window_name(worktree: Worktree) -> str:
    """tmux window name for a Worktree's Workspace (stable + filesystem-safe).

    Uses the sanitized branch so the same Worktree always resolves to the same
    window target for both ``new_window`` and ``kill_window``. Falls back to the
    worktree directory name when the branch is empty (e.g. detached HEAD).
    """
    if worktree.branch:
        return git.sanitize_branch(worktree.branch)
    return worktree.path.name


def _claude_command(worktree: Worktree, config: Config, resume: bool) -> str:
    """Build the command run in the left pane: resume the latest Session or start fresh."""
    if resume and worktree.latest_session is not None:
        return f"{config.claude_cmd} --resume {worktree.latest_session.session_id}"
    return config.claude_cmd


def activate(worktree: Worktree, config: Config, resume: bool = True) -> None:
    """Build the Workspace for a Worktree and launch (optionally resume) claude, then jump.

    One tmux session per Project (name == ``worktree.project_name``); the Workspace is a
    new window in that session laid out as claude-left / yazi-top-right / shell-bottom-right.
    The left pane runs ``claude --resume <session_id>`` when ``resume`` is set and the
    worktree has a most-recent Session, otherwise ``config.claude_cmd``. Finally jump to it.
    """
    session_name = worktree.project_name
    claude_cmd = _claude_command(worktree, config, resume)

    tmux.ensure_session(session_name, worktree.path)
    window_target = tmux.new_window(session_name, _window_name(worktree), worktree.path)
    tmux.build_workspace_layout(session_name, window_target, worktree.path, claude_cmd)

    worktree.lifecycle = Lifecycle.LIVE

    tmux.jump_to(session_name, window_target)


def new_worktree(project: Project, branch: str, config: Config) -> Worktree:
    """Create a new git worktree (DORMANT) and activate it.

    Delegates path construction + branch sanitization to ``git.add_worktree`` (which
    expands ``config.worktree_pattern`` off ``config.base_branch``), builds the Dormant
    Worktree, then Activates it to Live.
    """
    path = git.add_worktree(project.root, branch, config.worktree_pattern, config.base_branch)

    worktree = Worktree(
        project_name=project.name,
        path=path,
        branch=branch,
        is_primary=False,
        slug=sessions.encode_slug(path),
        lifecycle=Lifecycle.DORMANT,
    )

    activate(worktree, config)
    return worktree


def close_workspace(worktree: Worktree) -> None:
    """Kill the Worktree's tmux window only; never touch git.

    Returns the Worktree to Dormant. Safe against a missing window: tmux errors from
    ``kill_window`` are swallowed so closing an already-gone Workspace is a no-op.
    """
    try:
        tmux.kill_window(worktree.project_name, _window_name(worktree))
    except Exception:
        # The Workspace may already be gone; closing is idempotent. Never touch git.
        pass

    worktree.live = None
    worktree.lifecycle = Lifecycle.DORMANT


def remove_worktree(worktree: Worktree) -> None:
    """Remove the git worktree; destructive, caller must confirm.

    Only runs when explicitly called. Close the Workspace first so no window lingers
    pointing at a directory git is about to remove, then ``git worktree remove``.
    """
    close_workspace(worktree)
    git.remove_worktree(worktree.path)
