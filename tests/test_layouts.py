"""Unit tests for the pure layout plans (claude_mux.layouts)."""
from __future__ import annotations

from claude_mux import layouts


def test_classic_structure_and_command_injection():
    plan = layouts.build_plan("classic", "claude --resume x")
    assert plan.name == "classic"
    roles = [p.role for p in plan.panes]
    assert roles == ["claude", "yazi", "shell"]

    by_role = {p.role: p for p in plan.panes}
    # claude is the initial pane (no split) and carries the injected command.
    assert by_role["claude"].command == "claude --resume x"
    assert by_role["claude"].frm is None
    # yazi splits off the right half; shell splits below yazi; shell is a plain shell.
    assert (by_role["yazi"].frm, by_role["yazi"].direction) == ("claude", "right")
    assert (by_role["shell"].frm, by_role["shell"].direction) == ("yazi", "below")
    assert by_role["shell"].command is None


def test_dev_layout_has_lazygit_and_40pct_claude():
    plan = layouts.build_plan("dev", "claude")
    assert plan.name == "dev"
    by_role = {p.role: p for p in plan.panes}
    assert set(by_role) == {"claude", "yazi", "shell", "lazygit"}
    # claude keeps 40% => its right neighbour (yazi) splits off at 60%.
    assert by_role["yazi"].percentage == 60
    # bottom-right row: a plain terminal (shell) with lazygit split off it.
    assert by_role["shell"].command is None
    assert (by_role["lazygit"].frm, by_role["lazygit"].direction) == ("shell", "right")
    assert by_role["lazygit"].command == "lazygit"


def test_unknown_and_empty_layout_fall_back_to_default():
    assert layouts.build_plan("does-not-exist", "claude").name == layouts.DEFAULT_LAYOUT
    assert layouts.build_plan("", "claude").name == layouts.DEFAULT_LAYOUT
    # Case/whitespace tolerant.
    assert layouts.build_plan("  DEV  ", "claude").name == "dev"


def test_empty_claude_cmd_leaves_initial_pane_command_none():
    plan = layouts.build_plan("classic", "")
    assert plan.panes[0].command is None


def test_available_lists_builtins():
    assert layouts.available() == ["classic", "dev"]
