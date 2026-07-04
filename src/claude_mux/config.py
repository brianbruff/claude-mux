"""Configuration loading and saving for claude-mux.

Loading uses ``tomllib`` (stdlib); saving uses ``tomli_w``. Read-write config
edits only ``config.toml`` -- adding or removing a project NEVER creates,
deletes, or modifies any project directory or git data.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tomli_w

_CONFIG_HEADER = (
    "# claude-mux configuration\n"
    "# Managed by claude-mux. Hand edits are preserved on load but this file\n"
    "# is regenerated (comments except this header are dropped) on save.\n"
)


@dataclass
class Config:
    """User configuration loaded from config.toml."""

    projects: list[Path]
    worktree_pattern: str = "{repo}.worktrees/{branch}"
    base_branch: str = "HEAD"
    claude_cmd: str = "claude"
    # Appended to claude_cmd as ``--model <model>`` when set. Lets an operator pin
    # a model (e.g. "opus") without baking it into claude_cmd, and independently of
    # any enterprise default that overrides the on-disk claude config.
    model: Optional[str] = None
    # Name of the Workspace layout to build on Activate (see layouts.py).
    default_layout: str = "classic"
    # Command used to open a Worktree in an external editor (the ``o`` binding).
    # Split with shlex and the worktree path is appended, so ``code`` opens VS
    # Code; override to e.g. ``code -r`` or another editor's CLI.
    editor_cmd: str = "code"


def default_config_path() -> Path:
    """Return the default config path (~/.config/claude-mux/config.toml)."""
    return Path.home() / ".config" / "claude-mux" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Load Config from TOML; missing file -> Config([]) with defaults.

    Project paths have ``~`` expanded. The optional ``[defaults]`` table may
    override ``worktree_pattern``, ``base_branch``, ``claude_cmd``, ``model`` and
    ``default_layout``.
    """
    cfg_path = path if path is not None else default_config_path()

    if not cfg_path.exists():
        return Config(projects=[])

    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)

    projects = [
        Path(str(p)).expanduser()
        for p in data.get("projects", [])
    ]

    defaults = data.get("defaults", {})
    kwargs: dict[str, object] = {}
    if "worktree_pattern" in defaults:
        kwargs["worktree_pattern"] = str(defaults["worktree_pattern"])
    if "base_branch" in defaults:
        kwargs["base_branch"] = str(defaults["base_branch"])
    if "claude_cmd" in defaults:
        kwargs["claude_cmd"] = str(defaults["claude_cmd"])
    if "model" in defaults:
        kwargs["model"] = str(defaults["model"])
    if "default_layout" in defaults:
        kwargs["default_layout"] = str(defaults["default_layout"])
    if "editor_cmd" in defaults:
        kwargs["editor_cmd"] = str(defaults["editor_cmd"])

    return Config(projects=projects, **kwargs)  # type: ignore[arg-type]


def save_config(config: Config, path: Path | None = None) -> None:
    """Regenerate config.toml from ``config`` and write it atomically.

    Serializes the projects list and a ``[defaults]`` table via ``tomli_w``,
    prefixed with a fixed header comment. The parent directory is created if
    needed. The write is atomic (temp file in the same dir + ``os.replace``).
    """
    cfg_path = path if path is not None else default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    defaults: dict[str, object] = {
        "worktree_pattern": config.worktree_pattern,
        "base_branch": config.base_branch,
        "claude_cmd": config.claude_cmd,
        "default_layout": config.default_layout,
        "editor_cmd": config.editor_cmd,
    }
    # ``model`` is optional; TOML has no null, so only emit it when set.
    if config.model is not None:
        defaults["model"] = config.model

    data: dict[str, object] = {
        "projects": [str(p) for p in config.projects],
        "defaults": defaults,
    }

    body = tomli_w.dumps(data)
    contents = (_CONFIG_HEADER + "\n" + body).encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(
        dir=str(cfg_path.parent), prefix=".config-", suffix=".toml.tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, cfg_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _git_toplevel(project_root: Path) -> Path | None:
    """Return the git top-level of ``project_root``, or None if not a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    return Path(out).resolve()


def add_project(project_root: Path, path: Path | None = None) -> tuple[bool, str]:
    """Add ``project_root`` to the config after validating it is a git repo top-level.

    Expands ``~`` and resolves the path. Validates that it is a git repository
    whose top-level equals the resolved path (``git rev-parse --show-toplevel``).
    Dedupes against existing entries by resolved path. On success, appends and
    saves. Returns ``(added, human_message)``. The filesystem is untouched
    beyond ``config.toml``.
    """
    resolved = Path(project_root).expanduser().resolve()

    if not resolved.exists():
        return (False, f"Path does not exist: {resolved}")

    toplevel = _git_toplevel(resolved)
    if toplevel is None:
        return (False, f"Not a git repository: {resolved}")
    if toplevel != resolved:
        return (
            False,
            f"Not a git repository top-level: {resolved} "
            f"(top-level is {toplevel})",
        )

    config = load_config(path)
    existing = {p.expanduser().resolve() for p in config.projects}
    if resolved in existing:
        return (False, f"Project already added: {resolved}")

    config.projects.append(resolved)
    save_config(config, path)
    return (True, f"Added project: {resolved}")


def remove_project(project_root: Path, path: Path | None = None) -> bool:
    """Remove the matching project entry (by resolved path) and save.

    Returns True if an entry was removed. MUST NOT delete the project directory
    or any git data -- only ``config.toml`` is edited.
    """
    resolved = Path(project_root).expanduser().resolve()

    config = load_config(path)
    kept = [p for p in config.projects if p.expanduser().resolve() != resolved]
    if len(kept) == len(config.projects):
        return False

    config.projects = kept
    save_config(config, path)
    return True
