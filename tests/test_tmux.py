"""Unit tests for pure logic in claude_mux.tmux."""
from __future__ import annotations

from claude_mux.tmux import is_claude_command


def test_literal_claude_and_node():
    assert is_claude_command("claude") is True
    assert is_claude_command("node") is True
    assert is_claude_command("Claude") is True  # case-insensitive
    assert is_claude_command("  node  ") is True  # tolerant of whitespace


def test_semver_shaped_versions():
    assert is_claude_command("2.1.199") is True
    assert is_claude_command("2.1.197") is True
    assert is_claude_command("v2.1.0") is True
    assert is_claude_command("2.1") is True
    assert is_claude_command("10.20.30.40") is True


def test_non_claude_commands():
    assert is_claude_command("zsh") is False
    assert is_claude_command("bash") is False
    assert is_claude_command("python") is False
    assert is_claude_command("nodejs") is False  # not exactly 'node'
    assert is_claude_command("yazi") is False


def test_empty_and_garbage():
    assert is_claude_command("") is False
    assert is_claude_command("   ") is False
    assert is_claude_command("2.x.1") is False
    assert is_claude_command("v") is False
