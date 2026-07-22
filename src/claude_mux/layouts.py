"""Workspace layout plans (pure, no I/O).

A layout describes the panes of a Workspace window and how they are arranged. It
is pure data: the tmux executor (``tmux.build_workspace_layout``) turns a
``LayoutPlan`` into real splits and launches each pane's command.

A plan's first pane is the window's *initial* pane; every later pane is created
by splitting an earlier one (referenced by ``frm``). ``percentage`` is the size
of the *new* pane (matching libtmux's ``split(percentage=...)``), so a claude
pane at 40% is expressed as its right-hand neighbour splitting off at 60%.

Commands are plain strings with no shell wrapping, keeping the plan portable and
unit-testable. Shell reuse, launch ordering and terminal cleanup are the executor's
job.

Built-in layouts:
  * ``classic`` — claude top-left, codex bottom-left, yazi top-right, shell bottom-right.
  * ``dev``     — claude left (40%), yazi top-right, and a bottom-right row split
                  into a plain terminal and ``lazygit``.

Select one per install via ``[defaults] default_layout`` in config.toml.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_LAYOUT = "classic"


@dataclass(frozen=True)
class PaneSpec:
    """One pane in a layout.

    ``role`` is a unique key used in the returned ``{role: pane_id}`` map.
    ``command`` is the program to launch (``None`` leaves a plain interactive
    shell). ``frm`` names the pane this one splits from (``None`` only for the
    initial pane). ``direction`` is ``"right"`` or ``"below"``; ``percentage`` is
    the size of the new pane.
    """

    role: str
    command: Optional[str] = None
    frm: Optional[str] = None
    direction: Optional[str] = None
    percentage: Optional[int] = None


@dataclass(frozen=True)
class LayoutPlan:
    """An ordered set of panes; ``panes[0]`` is the window's initial pane."""

    name: str
    panes: tuple[PaneSpec, ...]


def _classic(claude_cmd: str) -> LayoutPlan:
    """claude top-left, codex bottom-left, yazi top-right, shell bottom-right."""
    return LayoutPlan(
        name="classic",
        panes=(
            PaneSpec("claude", claude_cmd or None),
            PaneSpec("yazi", "yazi", frm="claude", direction="right", percentage=50),
            PaneSpec("shell", None, frm="yazi", direction="below", percentage=50),
            PaneSpec("codex", "codex", frm="claude", direction="below", percentage=50),
        ),
    )


def _dev(claude_cmd: str) -> LayoutPlan:
    """claude left (40%), yazi top-right, bottom-right row = terminal + lazygit."""
    return LayoutPlan(
        name="dev",
        panes=(
            # claude keeps 40% because its right-hand neighbour splits off at 60%.
            PaneSpec("claude", claude_cmd or None),
            PaneSpec("yazi", "yazi", frm="claude", direction="right", percentage=60),
            PaneSpec("shell", None, frm="yazi", direction="below", percentage=50),
            PaneSpec("lazygit", "lazygit", frm="shell", direction="right", percentage=50),
        ),
    )


_BUILDERS: dict[str, Callable[[str], LayoutPlan]] = {
    "classic": _classic,
    "dev": _dev,
}


def available() -> list[str]:
    """Return the built-in layout names, sorted."""
    return sorted(_BUILDERS)


def build_plan(layout_name: str, claude_cmd: str) -> LayoutPlan:
    """Return the ``LayoutPlan`` for ``layout_name`` with ``claude_cmd`` injected.

    An empty or unknown name falls back to ``DEFAULT_LAYOUT`` (never raises) so a
    stray config value degrades to the known-good default rather than breaking
    Activate.
    """
    name = (layout_name or DEFAULT_LAYOUT).strip().lower()
    builder = _BUILDERS.get(name, _BUILDERS[DEFAULT_LAYOUT])
    return builder(claude_cmd)
