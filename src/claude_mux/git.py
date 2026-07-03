"""Git worktree operations via subprocess."""
from __future__ import annotations

import subprocess
from pathlib import Path

from claude_mux import sessions
from claude_mux.model import Lifecycle, Worktree


def sanitize_branch(branch: str) -> str:
    """Sanitize a branch name for filesystem and tmux-target use.

    Replaces every character that breaks a ``session:window`` tmux target with
    ``_``: ``/`` (path separator), ``.`` (tmux's ``window.pane`` separator) and
    ``:`` (tmux's ``session:window`` separator). Sanitizing these means the name
    round-trips — a window CREATED via ``new-window -n <name>`` can later be
    referenced by ``session:<name>`` (jump, kill) without tmux misparsing a dot
    as a pane index (e.g. ``feature.x`` -> window ``feature`` + pane ``x``).
    """
    for ch in ("/", ".", ":"):
        branch = branch.replace(ch, "_")
    return branch


def _run_git(cwd: Path, *args: str) -> str:
    """Run a git command in ``cwd`` and return stdout; raise on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _parse_worktree_porcelain(output: str, project_root: Path) -> list[Worktree]:
    """Parse ``git worktree list --porcelain`` output into Worktrees.

    Blank-line-separated blocks; the first block (and any ``bare`` block) is
    the primary working tree. ``branch refs/heads/<name>`` gives the branch;
    a ``detached`` block has no branch. Pure — no I/O.
    """
    project_name = project_root.name
    worktrees: list[Worktree] = []

    blocks = [b for b in output.split("\n\n") if b.strip()]
    for idx, block in enumerate(blocks):
        path: Path | None = None
        branch = ""
        is_bare = False
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):])
            elif line == "bare":
                is_bare = True
            elif line.startswith("branch "):
                ref = line[len("branch "):]
                if ref.startswith("refs/heads/"):
                    branch = ref[len("refs/heads/"):]
                else:
                    branch = ref
            elif line == "detached":
                branch = ""
        if path is None:
            continue
        worktrees.append(
            Worktree(
                project_name=project_name,
                path=path,
                branch=branch,
                is_primary=(idx == 0 or is_bare),
                slug=sessions.encode_slug(path),
                lifecycle=Lifecycle.DORMANT,
            )
        )
    return worktrees


def list_worktrees(project_root: Path) -> list[Worktree]:
    """Parse `git worktree list --porcelain` into Worktree objects."""
    output = _run_git(project_root, "worktree", "list", "--porcelain")
    return _parse_worktree_porcelain(output, project_root)


def add_worktree(project_root: Path, branch: str, pattern: str, base: str) -> Path:
    """Create a new git worktree and return its path.

    ``pattern`` is expanded with ``{repo}`` (``project_root.name``) and
    ``{branch}`` (the sanitized branch). A relative expansion is resolved as a
    sibling of ``project_root`` (i.e. relative to ``project_root.parent``),
    matching the ``<repo>.worktrees/<branch>`` convention. The git branch is
    created (``-b``) from the *unsanitized* branch name off ``base``.
    """
    expanded = pattern.format(repo=project_root.name, branch=sanitize_branch(branch))
    target = Path(expanded)
    if not target.is_absolute():
        target = project_root.parent / target

    _run_git(project_root, "worktree", "add", str(target), "-b", branch, base)
    return target


def remove_worktree(path: Path) -> None:
    """Remove a git worktree (git worktree remove). Destructive."""
    _run_git(path, "worktree", "remove", str(path))
