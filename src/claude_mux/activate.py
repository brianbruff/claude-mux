"""Lifecycle primitives: open a Worktree into a Workspace window + Claude.

claude-mux owns ONE tmux session (see ADR-0005 / M9). Each entered Worktree is a
full-screen window named ``<project>/<branch>`` in that session, carrying the
three-pane Workspace layout (claude-left / yazi-top-right / shell-bottom-right)
with ``claude`` auto-launched (resume-aware) in the left pane.

``open_or_select_workspace`` is the shared primitive: it create-or-selects the
Worktree's window and ``select-window``s to it (full-screen) — no ``switch-client``.

Layering:
  * ``new_worktree`` = create the git worktree, then open its Workspace.
  * ``close_workspace`` tears down only the tmux window (Live/Open -> Dormant); it
    NEVER touches git. The menu window (window 0) always survives.
  * ``remove_worktree`` is the distinct, destructive git teardown; callers confirm.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from claude_mux import git, layouts, sessions, tmux
from claude_mux.config import Config
from claude_mux.model import Lifecycle, Project, Worktree


def _workspace_window_name(project_name: str, worktree: Worktree) -> str:
    """tmux window name for a Worktree's Workspace: ``<project>/<branch>-<id>``.

    Grouping is UI-only now (ADR-0005): the project prefix keeps windows legible
    within the single owned session. Two properties are load-bearing for correct
    ``find_window``/``kill_window`` targeting:

    * **Round-trips as a tmux target.** The branch is passed through
      ``git.sanitize_branch`` so a ``.`` (tmux's ``window.pane`` separator) or
      ``:`` (its ``session:window`` separator) can never make ``session:<name>``
      misparse — e.g. branch ``release-2.5`` would otherwise create a window that
      ``kill_window`` cannot target (tmux reads ``.5`` as a pane index), leaking
      the window while the Worktree is flipped DORMANT. The project prefix is
      sanitized for the same reason.
    * **Globally unique per Worktree.** ``<project>/<branch>`` alone collides when
      two configured repos share a directory basename (Project.name is the dir
      basename) and hold the same branch, so ``find_window`` dedup would select
      the wrong project's window. A short digest of the Worktree's (unique) path
      disambiguates — the same key the app uses to tell same-basename Projects
      apart by identity (app._project_for). The digest is stable, so create and
      select/close all resolve to the same window.

    Falls back to the worktree directory name when the branch is empty (e.g.
    detached HEAD).
    """
    branch = worktree.branch or worktree.path.name
    disambiguator = hashlib.sha1(str(worktree.path).encode()).hexdigest()[:8]
    project = git.sanitize_branch(project_name)
    return f"{project}/{git.sanitize_branch(branch)}-{disambiguator}"


def _claude_command(worktree: Worktree, config: Config, resume: bool) -> str:
    """Build the command run in the left pane.

    ``claude_cmd`` [``--model <model>``] [``--resume <session_id>``]. The optional
    ``--model`` pins the model regardless of any enterprise default in the on-disk
    claude config; ``--resume`` is added when resuming the worktree's latest Session.
    """
    parts = [config.claude_cmd]
    if config.model:
        parts.append(f"--model {config.model}")
    if resume and worktree.latest_session is not None:
        parts.append(f"--resume {worktree.latest_session.session_id}")
    return " ".join(parts)


def _open_or_select(project_name: str, worktree: Worktree, config: Config, resume: bool) -> str:
    """Create-or-select the Worktree's Workspace window in the owned session.

    If a window named ``<project>/<branch>`` already exists, ``select-window`` to
    it (a second entry never spawns a duplicate window/claude). Otherwise build
    the three-pane layout, launch claude (resume-aware) in the left pane, then
    ``select-window`` (full-screen) + ``select-pane`` the claude pane. Returns the
    window target (``@``-prefixed window id). No ``switch-client`` anywhere.
    """
    tmux.ensure_menu_session()  # idempotent; guarantees the owned session exists
    name = _workspace_window_name(project_name, worktree)

    existing = tmux.find_window(tmux.MUX_SESSION, name)
    if existing is not None:
        tmux.jump_to(tmux.MUX_SESSION, window_target=existing)
        return existing

    claude_cmd = _claude_command(worktree, config, resume)
    plan = layouts.build_plan(config.default_layout, claude_cmd)
    window_target = tmux.new_window(tmux.MUX_SESSION, name, worktree.path)
    # Build the layout and launch the sibling panes (yazi/shell), but DEFER claude:
    # its startup terminal-capability handshake (anthropics/claude-code#17787) must
    # not race the split/select/resize churn, or the churn's DSR replies get read as
    # pre-typed input (the ``0n0n`` in the prompt).
    layout = tmux.build_workspace_layout(
        tmux.MUX_SESSION, window_target, worktree.path, plan, launch_first=False
    )

    worktree.lifecycle = Lifecycle.LIVE
    # Make the window full-screen and select the claude pane FIRST, so the pane has
    # its final geometry and the terminal is quiet, THEN launch claude last: the
    # respawn's ``-k`` flushes any stray replies before claude starts reading.
    tmux.jump_to(tmux.MUX_SESSION, window_target=window_target, pane_id=layout.get("claude"))
    first = plan.panes[0]
    tmux.launch_in_pane(
        layout[first.role], first.command or "", settle=tmux.CLAUDE_LAUNCH_SETTLE
    )
    return window_target


def open_or_select_workspace(
    project: Project, worktree: Worktree, config: Config, resume: bool = True
) -> str:
    """M9 entry primitive: open (create) or select the Worktree's Workspace window.

    Full-screen swap within the owned ``claude-mux`` session (ADR-0005). Returns
    the window target.
    """
    return _open_or_select(project.name, worktree, config, resume)


def activate(worktree: Worktree, config: Config, resume: bool = True) -> None:
    """Open (or select) a Worktree's Workspace in the owned session.

    Compatibility wrapper over ``open_or_select_workspace`` for callers that only
    hold the Worktree (its ``project_name`` names the window prefix).
    """
    _open_or_select(worktree.project_name, worktree, config, resume)


def new_worktree(
    project: Project, branch: str, config: Config, base: Optional[str] = None
) -> Worktree:
    """Create a new git worktree (DORMANT) and activate it.

    Delegates path construction + branch sanitization to ``git.add_worktree`` (which
    expands ``config.worktree_pattern``), builds the Dormant Worktree, then Activates
    it to Live. ``base`` is the commit-ish the new branch is created off; when omitted
    it falls back to ``config.base_branch`` (preserving the pre-dropdown behaviour).
    """
    path = git.add_worktree(
        project.root, branch, config.worktree_pattern, base or config.base_branch
    )

    worktree = Worktree(
        project_name=project.name,
        path=path,
        branch=branch,
        is_primary=False,
        slug=sessions.encode_slug(path),
        lifecycle=Lifecycle.DORMANT,
    )

    open_or_select_workspace(project, worktree, config)
    return worktree


def close_workspace(worktree: Worktree) -> None:
    """Kill the Worktree's tmux window only; never touch git.

    Returns the Worktree to Dormant. Safe against a missing window: tmux errors from
    ``kill_window`` are swallowed so closing an already-gone Workspace is a no-op.
    """
    try:
        tmux.kill_window(tmux.MUX_SESSION, _workspace_window_name(worktree.project_name, worktree))
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
