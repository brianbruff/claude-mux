"""Unit tests for read-write config (save_config, add_project, remove_project).

Adding/removing a project only edits config.toml; it never touches the git repo
or the project directory on disk.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from claude_mux import config as config_mod
from claude_mux.config import (
    Config,
    add_project,
    load_config,
    remove_project,
    save_config,
)


def _git_init(path: Path) -> Path:
    """Create a real git repo at ``path`` and return its resolved path."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    return path.resolve()


# --- save/load round-trip ---------------------------------------------------

def test_save_load_round_trip_preserves_projects_and_defaults(tmp_path: Path):
    cfg_path = tmp_path / "config.toml"
    projects = [tmp_path / "repo-a", tmp_path / "repo-b"]
    config = Config(
        projects=projects,
        worktree_pattern="{repo}.wt/{branch}",
        base_branch="main",
        claude_cmd="claude-dev",
    )

    save_config(config, cfg_path)
    loaded = load_config(cfg_path)

    assert loaded.projects == projects
    assert loaded.worktree_pattern == "{repo}.wt/{branch}"
    assert loaded.base_branch == "main"
    assert loaded.claude_cmd == "claude-dev"


def test_save_creates_parent_dir_and_header(tmp_path: Path):
    cfg_path = tmp_path / "nested" / "deeper" / "config.toml"
    save_config(Config(projects=[]), cfg_path)

    assert cfg_path.exists()
    text = cfg_path.read_text()
    assert text.startswith("# claude-mux configuration")


def test_save_is_atomic_no_temp_leftovers(tmp_path: Path):
    cfg_path = tmp_path / "config.toml"
    save_config(Config(projects=[tmp_path / "r"]), cfg_path)

    leftovers = list(tmp_path.glob(".config-*"))
    assert leftovers == []


# --- add_project ------------------------------------------------------------

def test_add_project_rejects_non_git_dir(tmp_path: Path):
    cfg_path = tmp_path / "config.toml"
    plain = tmp_path / "plain"
    plain.mkdir()

    added, msg = add_project(plain, cfg_path)

    assert added is False
    assert "git" in msg.lower()
    # Nothing was written / no project recorded.
    assert load_config(cfg_path).projects == []


def test_add_project_accepts_git_repo(tmp_path: Path):
    cfg_path = tmp_path / "config.toml"
    repo = _git_init(tmp_path / "myrepo")

    added, msg = add_project(repo, cfg_path)

    assert added is True
    assert load_config(cfg_path).projects == [repo]


def test_add_project_dedupes_by_resolved_path(tmp_path: Path):
    cfg_path = tmp_path / "config.toml"
    repo = _git_init(tmp_path / "myrepo")

    first_added, _ = add_project(repo, cfg_path)
    # Second add via a non-normalized path pointing at the same repo.
    second_added, msg = add_project(repo / "." / "", cfg_path)

    assert first_added is True
    assert second_added is False
    assert "already" in msg.lower()
    assert load_config(cfg_path).projects == [repo]


# --- remove_project ---------------------------------------------------------

def test_remove_project_removes_only_entry_and_leaves_folder(tmp_path: Path):
    cfg_path = tmp_path / "config.toml"
    keep = _git_init(tmp_path / "keep")
    drop = _git_init(tmp_path / "drop")

    add_project(keep, cfg_path)
    add_project(drop, cfg_path)
    assert set(load_config(cfg_path).projects) == {keep, drop}

    removed = remove_project(drop, cfg_path)

    assert removed is True
    assert load_config(cfg_path).projects == [keep]
    # The real folder and its git data are untouched.
    assert drop.exists()
    assert (drop / ".git").exists()


def test_remove_project_returns_false_when_absent(tmp_path: Path):
    cfg_path = tmp_path / "config.toml"
    keep = _git_init(tmp_path / "keep")
    add_project(keep, cfg_path)

    removed = remove_project(tmp_path / "never-added", cfg_path)

    assert removed is False
    assert load_config(cfg_path).projects == [keep]
