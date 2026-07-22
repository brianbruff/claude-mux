"""Unit tests for pure logic in claude_mux.tmux."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pathlib import Path

from claude_mux import tmux as tmux_mod
from claude_mux.model import AgentKind
from claude_mux.tmux import (
    PaneInfo,
    ProcSnapshot,
    _cmd_failed,
    classify_agent,
    classify_command_line,
    classify_pane,
    is_claude_command,
    launch_command_string,
)


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


# --- classify_agent ---------------------------------------------------------
# is_claude_command is now a thin wrapper over classify_agent; these lock the
# multi-agent classification and prove the wrapper is unchanged for claude.


def test_classify_claude_disguises():
    assert classify_agent("claude") is AgentKind.CLAUDE
    assert classify_agent("Claude") is AgentKind.CLAUDE  # case-insensitive
    assert classify_agent("  node  ") is AgentKind.CLAUDE  # runtime disguise, whitespace
    assert classify_agent("2.1.199") is AgentKind.CLAUDE  # semver disguise
    assert classify_agent("v2.1.0") is AgentKind.CLAUDE


def test_classify_other_agents():
    assert classify_agent("gemini") is AgentKind.GEMINI
    assert classify_agent("codex") is AgentKind.CODEX
    assert classify_agent("copilot") is AgentKind.COPILOT
    assert classify_agent("opencode") is AgentKind.OPENCODE
    # case / whitespace tolerant
    assert classify_agent("  GEMINI ") is AgentKind.GEMINI


def test_classify_non_agents_are_none():
    for cmd in ("zsh", "bash", "python", "nodejs", "yazi", "", "   "):
        assert classify_agent(cmd) is None


def test_is_claude_command_matches_classify():
    # The wrapper agrees with classify_agent for the claude cases only.
    assert is_claude_command("claude") is True
    assert is_claude_command("gemini") is False
    assert is_claude_command("zsh") is False


# --- classify_command_line --------------------------------------------------
# argv0 alone cannot tell claude from the other node-hosted agents; the full
# command line can, via the hosted script's basename.


def test_classify_command_line_direct_binaries():
    assert classify_command_line("claude --model opus") is AgentKind.CLAUDE
    assert classify_command_line("codex") is AgentKind.CODEX
    assert classify_command_line("/opt/homebrew/bin/copilot") is AgentKind.COPILOT


def test_classify_command_line_node_hosted_agents():
    # The regression: copilot runs as `node <path>/copilot`, not `copilot`.
    assert classify_command_line("node /opt/homebrew/bin/copilot") is AgentKind.COPILOT
    assert classify_command_line("node /usr/local/bin/gemini") is AgentKind.GEMINI
    assert classify_command_line("node --enable-source-maps /x/opencode.js") is AgentKind.OPENCODE


def test_classify_command_line_claude_and_bare_node_stay_claude():
    # claude under node, and any unrecognised bare-node process, remain CLAUDE
    # (matching the argv0-only fallback — no worse than before).
    assert classify_command_line("node /Users/x/.claude/local/cli.js") is AgentKind.CLAUDE
    assert classify_command_line("node /Users/x/proj/node_modules/.bin/vite") is AgentKind.CLAUDE
    assert classify_command_line("2.1.204") is AgentKind.CLAUDE


def test_classify_command_line_non_agents_are_none():
    for cmd in ("zsh -i", "python app.py", "", "   "):
        assert classify_command_line(cmd) is None


# --- classify_pane ----------------------------------------------------------


def _pane(cmd: str, pid: int) -> PaneInfo:
    return PaneInfo(
        pane_id="%1",
        session_name="s",
        window_index=0,
        pane_index=0,
        current_command=cmd,
        pid=pid,
        current_path=Path("."),
    )


def test_classify_pane_disambiguates_node_copilot_from_claude():
    # Pane shell (100) forks `node <path>/copilot` (200); tmux only sees "node".
    procs = ProcSnapshot(
        args_by_pid={100: "-zsh", 200: "node /opt/homebrew/bin/copilot"},
        children={100: [200]},
    )
    assert classify_pane(_pane("node", 100), procs) is AgentKind.COPILOT


def test_classify_pane_semver_claude_stays_claude():
    procs = ProcSnapshot(
        args_by_pid={100: "/bin/zsh -i -c claude", 200: "claude --model opus"},
        children={100: [200]},
    )
    assert classify_pane(_pane("2.1.204", 100), procs) is AgentKind.CLAUDE


def test_classify_pane_exact_binary_skips_process_tree():
    # A literal agent argv0 is trusted outright — no tree walk, empty snapshot ok.
    empty = ProcSnapshot(args_by_pid={}, children={})
    assert classify_pane(_pane("codex", 100), empty) is AgentKind.CODEX
    assert classify_pane(_pane("copilot", 100), empty) is AgentKind.COPILOT


def test_classify_pane_falls_back_to_claude_when_tree_empty():
    # No descendants resolvable (ps failed) -> preserve argv0 fallback (CLAUDE).
    empty = ProcSnapshot(args_by_pid={}, children={})
    assert classify_pane(_pane("node", 100), empty) is AgentKind.CLAUDE


def test_classify_pane_non_agent_is_none():
    empty = ProcSnapshot(args_by_pid={}, children={})
    assert classify_pane(_pane("zsh", 100), empty) is None


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


def test_launch_in_pane_reuses_initialized_shell_and_clears_startup_input(monkeypatch):
    calls: list[tuple] = []

    class FakePane:
        def send_keys(self, command, enter=False):
            calls.append(("send_keys", command, enter))

    class FakeServer:
        panes = None

        def __init__(self):
            self.panes = self

        def get(self, **kwargs):
            calls.append(("get", kwargs))
            return FakePane()

        def cmd(self, *args):
            calls.append(("cmd", args))

    def fake_launch(server, pane_id, command):
        calls.append(("launch", server, pane_id, command))

    monkeypatch.setattr(tmux_mod, "_server", FakeServer)
    monkeypatch.setattr(tmux_mod, "_launch", fake_launch)
    monkeypatch.setattr(tmux_mod.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    tmux_mod.launch_in_pane("%1", "claude", settle=0.15, reuse_shell=True)

    assert calls[0] == ("sleep", 0.15)
    assert calls[1] == ("get", {"pane_id": "%1", "default": None})
    assert calls[2] == ("cmd", ("send-keys", "-t", "%1", "C-u"))
    assert calls[3] == ("send_keys", "claude", True)
    assert not any(call[0] == "launch" for call in calls)
