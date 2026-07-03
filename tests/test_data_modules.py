"""Unit tests for pure logic in the 'data' module group.

Covers slug encoding (sessions), branch sanitizing and worktree porcelain
parsing (git), and config loading (config).
"""
from __future__ import annotations

from pathlib import Path

from claude_mux import config as config_mod
from claude_mux import git, sessions
from claude_mux.model import Lifecycle


# --- sessions.encode_slug ---------------------------------------------------

def test_encode_slug_basic():
    assert sessions.encode_slug(Path("/A/b c")) == "-A-b-c"


def test_encode_slug_dots_and_hyphens():
    p = Path("/Users/brian.keating/Data/Repo/github/claude-mux")
    assert sessions.encode_slug(p) == "-Users-brian-keating-Data-Repo-github-claude-mux"


def test_encode_slug_worktree_path():
    p = Path("/Users/brian.keating/Data/Repo/github/claude-code-demo.worktrees/styling")
    assert (
        sessions.encode_slug(p)
        == "-Users-brian-keating-Data-Repo-github-claude-code-demo-worktrees-styling"
    )


# --- git.sanitize_branch ----------------------------------------------------

def test_sanitize_branch_replaces_slash():
    assert git.sanitize_branch("feature/awesomefeat") == "feature_awesomefeat"


def test_sanitize_branch_multiple_slashes():
    assert git.sanitize_branch("a/b/c") == "a_b_c"


def test_sanitize_branch_noop():
    assert git.sanitize_branch("main") == "main"


# --- git._parse_worktree_porcelain ------------------------------------------

PORCELAIN = """\
worktree /Users/me/repo
HEAD abc123
branch refs/heads/main

worktree /Users/me/repo.worktrees/feature_x
HEAD def456
branch refs/heads/feature/x

worktree /Users/me/repo.worktrees/detached
HEAD 999888
detached
"""


def test_parse_porcelain_count_and_primary():
    wts = git._parse_worktree_porcelain(PORCELAIN, Path("/Users/me/repo"))
    assert len(wts) == 3
    assert wts[0].is_primary is True
    assert wts[1].is_primary is False
    assert wts[2].is_primary is False


def test_parse_porcelain_branch_and_path():
    wts = git._parse_worktree_porcelain(PORCELAIN, Path("/Users/me/repo"))
    assert wts[0].branch == "main"
    assert wts[1].branch == "feature/x"
    assert wts[1].path == Path("/Users/me/repo.worktrees/feature_x")


def test_parse_porcelain_detached_has_no_branch():
    wts = git._parse_worktree_porcelain(PORCELAIN, Path("/Users/me/repo"))
    assert wts[2].branch == ""


def test_parse_porcelain_metadata():
    wts = git._parse_worktree_porcelain(PORCELAIN, Path("/Users/me/repo"))
    assert wts[0].project_name == "repo"
    assert wts[0].lifecycle is Lifecycle.DORMANT
    assert wts[0].slug == sessions.encode_slug(Path("/Users/me/repo"))


def test_parse_porcelain_bare_is_primary():
    bare = "worktree /Users/me/repo\nbare\n"
    wts = git._parse_worktree_porcelain(bare, Path("/Users/me/repo"))
    assert len(wts) == 1
    assert wts[0].is_primary is True


# --- config.load_config -----------------------------------------------------

def test_load_config_missing_file(tmp_path):
    cfg = config_mod.load_config(tmp_path / "nope.toml")
    assert cfg.projects == []
    assert cfg.worktree_pattern == "{repo}.worktrees/{branch}"
    assert cfg.base_branch == "HEAD"
    assert cfg.claude_cmd == "claude"


def test_load_config_reads_projects_and_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'projects = ["~/foo", "/abs/bar"]\n'
        "\n"
        "[defaults]\n"
        'base_branch = "develop"\n'
        'claude_cmd = "claude --dangerously"\n'
    )
    cfg = config_mod.load_config(p)
    assert cfg.projects[0] == (Path.home() / "foo")
    assert cfg.projects[1] == Path("/abs/bar")
    assert cfg.base_branch == "develop"
    assert cfg.claude_cmd == "claude --dangerously"
    # untouched default
    assert cfg.worktree_pattern == "{repo}.worktrees/{branch}"


def test_default_config_path():
    assert config_mod.default_config_path() == (
        Path.home() / ".config" / "claude-mux" / "config.toml"
    )
