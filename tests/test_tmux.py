"""Unit tests for pure logic in claude_mux.tmux."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from claude_mux.tmux import _cmd_failed, is_claude_command, launch_command_string


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


# --- launch_command_string --------------------------------------------------
# The command is run in an INTERACTIVE shell so aliases / functions / rc-PATH
# (e.g. a `george`-style alias or an nvm-installed `claude`) resolve as they did
# under the old send_keys path, while still avoiding the 0n0n leak.


def test_launch_uses_interactive_shell():
    s = launch_command_string("claude --model opus")
    assert " -i -c " in s  # interactive shell so ~/.zshrc / aliases are sourced
    assert "claude --model opus" in s
    # $SHELL (with sh fallback) is used, not a hardcoded shell.
    assert "${SHELL:-/bin/sh}" in s


def test_launch_keeps_shell_after_exit():
    # Trailing `exec $SHELL` leaves an interactive shell when the command exits.
    s = launch_command_string("claude")
    assert s.count("${SHELL:-/bin/sh}") >= 2
    assert "; exec ${SHELL:-/bin/sh}" in s


def test_launch_quotes_the_inner_command():
    # A command with shell metacharacters must be a single quoted -c argument, so
    # the outer shell can't reinterpret it.
    s = launch_command_string("claude; rm -rf /")
    # The whole inner payload is single-quoted after `-c `.
    assert " -c '" in s
    assert s.endswith("'")


@dataclass
class _FakeResult:
    returncode: Optional[int] = 0
    stderr: Optional[list] = None


def test_cmd_failed_detects_failure_modes():
    # Success: rc 0, no stderr.
    assert _cmd_failed(_FakeResult(returncode=0, stderr=[])) is False
    assert _cmd_failed(_FakeResult(returncode=None, stderr=None)) is False
    # Failure: non-zero rc, or stderr present, or no result at all.
    assert _cmd_failed(_FakeResult(returncode=1, stderr=[])) is True
    assert _cmd_failed(_FakeResult(returncode=0, stderr=["can't find pane"])) is True
    assert _cmd_failed(None) is True
