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


# Preference order for the base a new worktree branches off. ``develop`` first
# because in a git-flow repo that is where features branch from; ``main``/``master``
# next for GitHub-flow / trunk-based repos that have no ``develop``. This is a
# deliberate replacement for basing off ``HEAD`` (whatever happens to be checked
# out), which silently branches a feature off ``main`` right after a release.
PREFERRED_BASE_BRANCHES = ("develop", "main", "master")


def list_branches(project_root: Path) -> list[str]:
    """Return local branch names, in git's own ordering, with no markers.

    Pure listing via ``git branch --format`` — no current-branch ``*`` prefix to
    strip and no leading whitespace, so each line is a usable branch name.
    """
    output = _run_git(project_root, "branch", "--format=%(refname:short)")
    return [line.strip() for line in output.splitlines() if line.strip()]


def default_base_branch(project_root: Path, fallback: str = "HEAD") -> str:
    """Pick the base branch a new worktree should default to.

    Prefers ``develop`` (git-flow feature base), then ``main``/``master`` (trunk),
    falling back to ``fallback`` when none of those exist — e.g. an unusual or
    freshly-initialised repo. ``fallback`` is normally ``config.base_branch`` so an
    explicitly-configured base still wins when the preferred branches are absent.
    """
    branches = set(list_branches(project_root))
    for name in PREFERRED_BASE_BRANCHES:
        if name in branches:
            return name
    return fallback


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


def remove_worktree(project_root: Path, path: Path) -> None:
    """Remove a git worktree, tolerating an already-deleted directory. Destructive.

    Runs from ``project_root`` — never from ``path``, which may already be gone if
    the operator deleted the folder by hand (subprocess cannot ``cwd`` into a
    missing directory). ``--force`` covers a worktree whose directory is missing
    or whose state is dirty/locked. If git still refuses (a dangling half-removed
    entry), fall back to ``git worktree prune`` to clear the stale registration so
    it stops appearing in ``git worktree list``.
    """
    try:
        _run_git(project_root, "worktree", "remove", "--force", str(path))
    except subprocess.CalledProcessError:
        _run_git(project_root, "worktree", "prune")


def delete_branch(project_root: Path, branch: str) -> None:
    """Force-delete a local branch (``git branch -D``). Destructive.

    Force (``-D``, not ``-d``) so a feature branch that was never merged is still
    removed — deleting the worktree is the operator's signal that the branch is
    done with. Must run AFTER the worktree is removed: git refuses to delete a
    branch that is still checked out in a worktree.
    """
    _run_git(project_root, "branch", "-D", branch)
