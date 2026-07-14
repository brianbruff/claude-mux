"""Textual dashboard UI: a collapsible Tree of Projects and Worktrees.

The tree's backbone is Projects -> Worktrees (CONTEXT.md). A Live Claude is a
status attribute of a Worktree row, not a node of its own. Rows show lifecycle,
activity, the Session summary, and any Footer Scrape extras that were captured.

Refresh is event-driven with a slow safety poll (ADR-0001/0004): a ~4s interval
runs ``StatusEngine.snapshot`` off the UI thread, and a faster ~1s tick watches
the Events File mtime so a ``waiting`` transition surfaces almost immediately.
"""
from __future__ import annotations

import shlex
import socket
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Header, Input, Label, Select, Static, Tree

from claude_mux import activate, git, panemap, picker, treestate
from claude_mux.config import Config, add_project, load_config, remove_project
from claude_mux.model import Activity, AgentKind, Lifecycle, LiveAgent, Project, Worktree
from claude_mux.status import StatusEngine
from claude_mux import tmux


# --------------------------------------------------------------------------- #
# Pure label formatting (no I/O — unit tested)                                 #
# --------------------------------------------------------------------------- #

_LIFECYCLE_MARKER = {
    Lifecycle.DORMANT: "○",  # ○ hollow: on disk, no window
    Lifecycle.OPEN: "◐",     # ◐ half: window but no claude
    Lifecycle.LIVE: "●",     # ● solid: running claude
}

_ACTIVITY_LABEL = {
    Activity.RUNNING: "running",
    Activity.WAITING: "waiting",
    Activity.IDLE: "idle",
    Activity.UNKNOWN: "",
}


def format_idle(seconds: Optional[int]) -> Optional[str]:
    """Render an idle age as a short human string (``idle 3m`` / ``idle 12s``)."""
    if seconds is None:
        return None
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"idle {seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"idle {minutes}m"
    hours = minutes // 60
    return f"idle {hours}h"


def activity_text(live: Optional[LiveAgent]) -> str:
    """Short activity descriptor for a Live Claude (empty if none/unknown)."""
    if live is None:
        return ""
    if live.activity is Activity.IDLE:
        return format_idle(live.idle_seconds) or "idle"
    return _ACTIVITY_LABEL.get(live.activity, "")


def scrape_extras(live: Optional[LiveAgent]) -> list[str]:
    """Footer Scrape extras as display chips: model, context %, cost, elapsed."""
    if live is None:
        return []
    parts: list[str] = []
    if live.model:
        parts.append(live.model)
    if live.context_pct is not None:
        parts.append(f"{live.context_pct}%")
    if live.cost_usd is not None:
        parts.append(f"${live.cost_usd:.2f}")
    if live.elapsed:
        parts.append(str(live.elapsed))
    return parts


def worktree_summary(wt: Worktree) -> str:
    """Best available 'what is this Claude doing' text for a worktree.

    The ``latest_session`` fallback is the *claude* Session Index for the slug, so
    it may only be shown when the primary agent is claude (or absent) — a
    gemini/codex-only worktree must not display a stale claude conversation title.
    """
    if wt.live is not None and wt.live.summary:
        return wt.live.summary
    primary_is_claude = wt.live is None or wt.live.kind is AgentKind.CLAUDE
    if primary_is_claude and wt.latest_session is not None and wt.latest_session.summary:
        return wt.latest_session.summary
    return ""


def worktree_label(wt: Worktree) -> str:
    """Compose the single-line label for a Worktree row.

    Format: ``<marker> <branch>[*]  <activity>  <summary>  <extras>`` — sections
    are only added when they carry information, so a Dormant worktree with no
    prior Session collapses to just ``○ branch``.
    """
    marker = _LIFECYCLE_MARKER.get(wt.lifecycle, "?")
    name = wt.branch or wt.path.name
    if wt.is_primary:
        name = f"{name} *"
    segments: list[str] = [f"{marker} {name}"]

    activity = activity_text(wt.live)
    if activity:
        segments.append(activity)

    summary = worktree_summary(wt)
    if summary:
        segments.append(summary)

    extras = scrape_extras(wt.live)
    if extras:
        segments.append(" · ".join(extras))

    return "   ".join(segments)


def project_label(project: Project) -> str:
    """Compose the label for a Project node (name + worktree count)."""
    count = len(project.worktrees)
    suffix = "worktree" if count == 1 else "worktrees"
    return f"{project.name}  ({count} {suffix})"


# --------------------------------------------------------------------------- #
# Display palette + Rich label rendering (claude-mux TUI.dc.html)              #
# --------------------------------------------------------------------------- #
# One authority accent — Enverus green — carries running state, active selection
# and the primary CTA; amber is caution, blue is info/idle, red is failure.
# Hairlines and surface contrast do the separation work, not a loud green bar.
# The pure ``*_label`` helpers above stay plain text (unit tested); the
# ``*_rich_label`` builders below add colour for on-screen rendering only.

_GREEN = "#6cbf3f"        # running / live / primary accent
_AMBER = "#d9a441"        # caution / waiting / uncommitted
_BLUE = "#3d9bd4"         # info / idle / open workspace
_RED = "#d16565"          # failure
_TEXT_BRIGHT = "#eef2f4"  # active-row branch name
_TEXT = "#c7cdd3"         # body text
_MUTED = "#9aa1a8"        # summaries, secondary metadata
_DIM = "#7f868d"          # tertiary
_FAINT = "#5f666c"        # dormant markers, primary flag
_SEP = "#4a5157"          # metadata dot separators


# Enverus-green dark theme. Registered on the App so it drives every stock
# widget at once — Header, Footer, the modals' ``$accent`` borders, the command
# palette (1e) and the keys/help panel (1f) all inherit these tokens.
CLAUDE_MUX_THEME = Theme(
    name="claude-mux",
    primary="#3C8321",     # authority green: running / selected / primary CTA
    secondary=_BLUE,       # info / idle
    accent=_GREEN,         # bright green highlights + focus rings
    success=_GREEN,
    warning=_AMBER,
    error=_RED,
    foreground=_TEXT,
    background="#101214",
    surface="#17191b",
    panel="#1b1d1f",
    dark=True,
    variables={
        "block-cursor-foreground": "#ffffff",
        "block-cursor-background": "#3C8321",
        "input-selection-background": "#3C8321 45%",
    },
)


def _marker_color(wt: Worktree) -> str:
    """Colour for a worktree's lifecycle marker, folding in live activity.

    Running is green (authority), waiting is amber (needs the operator), idle is
    blue, an open-but-quiet workspace is blue, and a dormant worktree is faint.
    """
    if wt.lifecycle is Lifecycle.LIVE:
        activity = wt.live.activity if wt.live else Activity.UNKNOWN
        if activity is Activity.WAITING:
            return _AMBER
        if activity is Activity.IDLE:
            return _BLUE
        return _GREEN
    if wt.lifecycle is Lifecycle.OPEN:
        return _BLUE
    return _FAINT


def worktree_rich_label(wt: Worktree) -> Text:
    """Coloured Rich label for a Worktree row (display mirror of ``worktree_label``).

    A running worktree gets a bold bright name and a green ``RUNNING`` tag; every
    other state stays quiet so the one running row is the thing the eye lands on.
    Built as a ``Text`` (not a markup string) so branch names / summaries with
    ``[`` never need escaping.
    """
    marker = _LIFECYCLE_MARKER.get(wt.lifecycle, "?")
    live = wt.live
    running = (
        wt.lifecycle is Lifecycle.LIVE
        and live is not None
        and live.activity is Activity.RUNNING
    )

    label = Text(no_wrap=True)
    label.append(f"{marker} ", style=_marker_color(wt))
    name = wt.branch or wt.path.name
    label.append(name, style=f"bold {_TEXT_BRIGHT}" if running else _TEXT)
    if wt.is_primary:
        label.append(" *", style=_FAINT)

    # Multiple agents in one worktree: collapse the single-agent tail into a
    # compact count badge (the per-agent detail lives in the side panel). Guarded
    # on LIVE so a stale agents list on a closed worktree can never show a badge.
    if wt.lifecycle is Lifecycle.LIVE and len(wt.agents) > 1:
        waiting = any(a.activity is Activity.WAITING for a in wt.agents)
        running_any = any(a.activity is Activity.RUNNING for a in wt.agents)
        badge_style = _AMBER if waiting else (_GREEN if running_any else _BLUE)
        label.append(f"  {len(wt.agents)} agents", style=badge_style)
        return label

    if running:
        label.append("  RUNNING", style=f"bold {_GREEN}")
    else:
        activity = activity_text(live)
        if activity:
            waiting = live is not None and live.activity is Activity.WAITING
            label.append(f"  {activity}", style=_AMBER if waiting else _MUTED)

    summary = worktree_summary(wt)
    if summary:
        label.append(f"  {summary}", style=_MUTED)

    extras = scrape_extras(live)
    if extras:
        label.append("  ")
        for i, extra in enumerate(extras):
            if i:
                label.append(" · ", style=_SEP)
            label.append(extra, style="#dfe4e8" if running else _MUTED)

    return label


def project_rich_label(project: Project) -> Text:
    """Coloured Rich label for a Project node (name + worktree count)."""
    count = len(project.worktrees)
    suffix = "worktree" if count == 1 else "worktrees"
    label = Text(no_wrap=True)
    label.append(project.name, style=f"bold {_MUTED}")
    label.append(f"  · {count} {suffix}", style=_FAINT)
    return label


# --------------------------------------------------------------------------- #
# Detail panel rendering (the side panel, one block per agent in a worktree)   #
# --------------------------------------------------------------------------- #
# Pure builders (no App, no I/O) so they unit-test as plain Rich renderables.

# Per-kind glyph for the detail panel. claude keeps the solid dot family; the
# others get distinct shapes so a mixed worktree reads at a glance.
_AGENT_GLYPH = {
    AgentKind.CLAUDE: "◆",
    AgentKind.GEMINI: "✦",
    AgentKind.CODEX: "◇",
    AgentKind.COPILOT: "▲",
    AgentKind.OPENCODE: "■",
    AgentKind.UNKNOWN: "●",
}


def _agent_color(agent: LiveAgent) -> str:
    """Activity-driven colour for one agent's marker (per-agent mirror of _marker_color)."""
    if agent.activity is Activity.WAITING:
        return _AMBER
    if agent.activity is Activity.IDLE:
        return _BLUE
    if agent.activity is Activity.RUNNING:
        return _GREEN
    return _DIM  # UNKNOWN (e.g. a scrape-only non-claude agent)


def agent_block(agent: LiveAgent) -> Text:
    """One agent's detail block: glyph + KIND + activity, summary (claude), chips."""
    block = Text(no_wrap=False)
    glyph = _AGENT_GLYPH.get(agent.kind, "●")
    block.append(f"{glyph} ", style=_agent_color(agent))
    block.append(agent.kind.value, style=f"bold {_TEXT_BRIGHT}")

    activity = activity_text(agent)
    if activity:
        waiting = agent.activity is Activity.WAITING
        block.append(f"   {activity}", style=_AMBER if waiting else _MUTED)

    if agent.summary:  # only claude carries a conversation summary
        block.append(f"\n    {agent.summary}", style=_MUTED)

    extras = scrape_extras(agent)
    if extras:
        block.append("\n    ")
        for i, extra in enumerate(extras):
            if i:
                block.append(" · ", style=_SEP)
            block.append(extra, style=_MUTED)
    return block


def worktree_detail(wt: Worktree) -> Group:
    """Full detail renderable for a Worktree: header + path + one block per agent."""
    header = Text(no_wrap=True)
    header.append(f"{_LIFECYCLE_MARKER.get(wt.lifecycle, '?')} ", style=_marker_color(wt))
    header.append(wt.branch or wt.path.name, style=f"bold {_TEXT_BRIGHT}")
    if wt.is_primary:
        header.append(" *", style=_FAINT)
    header.append(f"   {wt.lifecycle.value}", style=_DIM)

    path = Text(str(wt.path), style=_FAINT)

    if not wt.agents:
        hint = Text("No agent running — Enter to open, r to resume", style=_DIM)
        return Group(header, path, Text(""), hint)

    blocks: list = [header, path, Text("")]
    if len(wt.agents) > 1:
        blocks.append(Text(f"{len(wt.agents)} agents", style=_BLUE))
        blocks.append(Text(""))
    for agent in wt.agents:
        blocks.append(agent_block(agent))
        blocks.append(Text(""))
    return Group(*blocks)


def project_detail(project: Project) -> Text:
    """Detail renderable for a Project node: counts + lifecycle + per-kind rollup."""
    count = len(project.worktrees)
    suffix = "worktree" if count == 1 else "worktrees"
    text = Text(no_wrap=False)
    text.append(project.name, style=f"bold {_TEXT_BRIGHT}")
    text.append(f"   {count} {suffix}\n\n", style=_FAINT)

    live = sum(1 for w in project.worktrees if w.lifecycle is Lifecycle.LIVE)
    open_ = sum(1 for w in project.worktrees if w.lifecycle is Lifecycle.OPEN)
    dormant = count - live - open_
    text.append(f"● {live} live", style=_GREEN if live else _DIM)
    text.append("   ")
    text.append(f"◐ {open_} open", style=_BLUE if open_ else _DIM)
    text.append("   ")
    text.append(f"○ {dormant} dormant", style=_MUTED if dormant else _DIM)

    kinds: dict[AgentKind, int] = {}
    for w in project.worktrees:
        for agent in w.agents:
            kinds[agent.kind] = kinds.get(agent.kind, 0) + 1
    if kinds:
        text.append("\n\nagents: ", style=_DIM)
        parts = " · ".join(f"{n} {k.value}" for k, n in kinds.items())
        text.append(parts, style=_MUTED)
    return text


# --------------------------------------------------------------------------- #
# Modal dialogs                                                                #
# --------------------------------------------------------------------------- #


class BranchPromptScreen(ModalScreen[Optional[tuple[str, str]]]):
    """Prompt for a new worktree branch name and its base branch.

    Dismisses with ``(branch, base)`` on submit, or ``None`` on cancel. ``base`` is
    the commit-ish the new branch is created off — preselected to the git-flow-aware
    default (develop > main > master > configured fallback) and changeable via the
    dropdown for the exceptions (e.g. a hotfix off ``main``).
    """

    DEFAULT_CSS = """
    BranchPromptScreen {
        align: center middle;
    }
    BranchPromptScreen #dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    BranchPromptScreen Label {
        margin-bottom: 1;
    }
    BranchPromptScreen #base-label {
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, branches: list[str], default_base: str) -> None:
        """``branches`` are the selectable bases; ``default_base`` is preselected.

        ``default_base`` is guaranteed to be offered even if it is not in
        ``branches`` (e.g. the ``HEAD`` fallback for a repo with no branches yet).
        """
        super().__init__()
        options = list(branches)
        if default_base not in options:
            options.insert(0, default_base)
        self._options = options
        self._default_base = default_base

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("New worktree branch name:")
            yield Input(placeholder="feature/my-branch", id="branch")
            yield Label("Base branch:", id="base-label")
            yield Select(
                [(name, name) for name in self._options],
                value=self._default_base,
                allow_blank=False,
                id="base",
            )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        branch = self.query_one("#branch", Input).value.strip()
        if not branch:
            self.dismiss(None)
            return
        base = self.query_one("#base", Select).value
        self.dismiss((branch, base))

    def action_cancel(self) -> None:
        self.dismiss(None)


class PathPromptScreen(ModalScreen[Optional[str]]):
    """Prompt for a project directory path; dismisses with the path or None.

    The fallback for adding a project when ``yazi`` is not on PATH.
    """

    DEFAULT_CSS = """
    PathPromptScreen {
        align: center middle;
    }
    PathPromptScreen #dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    PathPromptScreen Label {
        margin-bottom: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Project directory (git repo root):")
            yield Input(placeholder="~/path/to/repo", id="path")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirmation; dismisses True only on an explicit yes."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    ConfirmScreen #dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    ConfirmScreen Label {
        margin-bottom: 1;
    }
    ConfirmScreen #buttons {
        height: auto;
        align-horizontal: right;
    }
    ConfirmScreen Button {
        margin-left: 2;
    }
    /* Tab/Shift+Tab cycle between No and Yes (Screen's built-in focus chain);
       recolour the focused button's top/bottom rule to the bright accent so the
       cursor is obvious. Only the top/bottom border is touched (it is already
       ``tall`` by default) so focus never shifts the button's size. */
    ConfirmScreen Button:focus {
        border-top: tall $accent;
        border-bottom: tall $accent;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("escape", "no", "No"),
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal(id="buttons"):
                yield Button("No", variant="primary", id="no")
                yield Button("Yes", variant="error", id="yes")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


# --------------------------------------------------------------------------- #
# The dashboard app                                                            #
# --------------------------------------------------------------------------- #


class ClaudeMuxApp(App):
    """Textual App rendering the Project -> Worktree tree with lifecycle bindings."""

    TITLE = "claude-mux"

    CSS = """
    Screen {
        background: $background;
    }

    Header {
        background: $panel;
        color: $foreground;
    }

    /* Body: the tree (left) beside the agent detail panel (right). */
    #body {
        height: 1fr;
    }

    /* The Project -> Worktree tree: quiet surface, hairline guides, and a
       green-tinted active row (the design's left-accent selection). */
    Tree {
        width: 60%;
        padding: 0 1;
        background: $surface;
        color: $foreground;
        scrollbar-background: $surface;
        scrollbar-color: #2a2e31;
    }

    /* Agent detail panel: a quieter surface than the tree, hairline divider,
       scrolls independently for worktrees hosting many agents. */
    #detail {
        width: 40%;
        padding: 0 1;
        background: $panel;
        color: $foreground;
        border-left: solid #2a2e31;
        scrollbar-background: $panel;
        scrollbar-color: #2a2e31;
    }
    #detail-body {
        height: auto;
    }
    Tree > .tree--cursor {
        background: $primary 20%;
        color: $text;
        text-style: bold;
    }
    Tree > .tree--highlight-line {
        background: $primary 8%;
    }
    Tree > .tree--guides {
        color: #2a2e31;
    }
    Tree > .tree--guides-selected {
        color: $accent;
    }
    Tree > .tree--guides-hover {
        color: $primary;
    }

    /* Quiet, informative status line — replaces the loud full-width green bar. */
    #statusline {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $foreground;
    }

    /* Green focus ring on the active input, per the modal spec (1d). */
    Input:focus {
        border: tall $accent;
    }
    """

    BINDINGS = [
        Binding("enter", "jump", "Enter workspace"),
        Binding("n", "new", "New worktree"),
        Binding("r", "resume", "Resume"),
        Binding("x", "close", "Close workspace"),
        Binding("a", "add_project", "Add project"),
        Binding("d", "remove_project", "Remove project"),
        Binding("g", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        # vim-style LEVEL navigation. The tree has two levels — Projects and
        # their Worktrees — and hjkl move within/between them rather than over
        # every visible row:
        #   * j/k step to the next/previous SIBLING at the current level (project
        #     -> project, or worktree -> worktree of the same project).
        #   * l steps IN: on a Project, into its first Worktree; on a Worktree it
        #     resumes that Worktree's session (same as Enter).
        #   * h steps OUT: from a Worktree back to its Project (and collapses a
        #     Project already at the top level).
        #   * o opens the selected Worktree in the configured editor (VS Code).
        # j/k are kept off the footer to avoid clutter. Single letters are safe
        # next to the modal Inputs: an Input consumes printable keys itself, so
        # these never fire while a BranchPrompt/PathPrompt is focused (same as
        # the existing n/r/x/a/d bindings).
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "step_in", "In / resume"),
        Binding("h", "step_out", "Out"),
        Binding("o", "open_editor", "Open in editor"),
    ]

    def __init__(self, config: Config, popup: bool = False):
        """Initialize the app with configuration and popup mode flag."""
        super().__init__()
        self.config = config
        self.popup = popup
        self.engine = StatusEngine(config)
        self._projects: list[Project] = []
        self._events_mtime: float = 0.0
        # Short hostname for the status line (drop any domain suffix).
        self._host = socket.gethostname().split(".")[0]
        # Authoritative set of collapsed Project roots, seeded from disk so the
        # operator's expand/collapse layout survives across sessions. Kept in
        # sync by the NodeCollapsed/NodeExpanded handlers (which fire for every
        # toggle path: Enter, h/l, mouse) and consumed by _rebuild_tree so a
        # background refresh never re-expands a collapsed Project.
        self._collapsed_projects: set[Path] = treestate.load_collapsed()
        # Monotonic generation bumped whenever the engine is swapped (config
        # add/remove). A refresh worker captures the generation current when it
        # starts; _rebuild_tree drops any result whose generation is stale, so a
        # slow in-flight snapshot taken against the OLD engine can never repaint
        # over the new config (ADR-0001 races; findings on stale repaint).
        self._refresh_gen: int = 0
        # Serializes the config workers' load->modify->save->reload sequence.
        # ``@work(thread=True, group="config")`` does NOT serialize threaded
        # workers, so a concurrent add+remove could interleave their
        # read-modify-write of config.toml and lose one update (last writer
        # wins). Holding this lock across the whole mutation (including the
        # reload/engine swap) makes the sequence atomic between the two workers.
        self._config_lock = threading.Lock()
        # Serializes the lifecycle workers' create-or-select of a Worktree's
        # Workspace window. ``@work(thread=True, group="lifecycle")`` does NOT
        # serialize threaded workers, so a double-tap of Enter (or Enter then a
        # re-open) starts two threads that both run activate's check-then-act
        # (find_window -> new_window) before either creates the window: both see
        # none and both build a window, spawning a duplicate window + duplicate
        # ``claude`` in the same worktree. Holding this lock across the whole
        # lifecycle op makes the find/create atomic, so the second worker re-runs
        # find_window under the lock and selects the now-existing window instead.
        self._lifecycle_lock = threading.Lock()
        # NB: not ``self.tree`` — Textual's App defines a read-only ``tree``
        # property (the DOM debug tree), so we hold the widget under ``_tree``.
        self._tree: Tree | None = None
        # Last renderable pushed to the detail panel (testability hook).
        self._detail_renderable = None

    # -- composition -------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Header()
        # Tree on the left, agent detail panel on the right. The panel lists every
        # agent in the selected worktree (or a project rollup) and scrolls on its own.
        with Horizontal(id="body"):
            yield Tree("Projects", id="tree")
            with VerticalScroll(id="detail"):
                yield Static(id="detail-body")
        # Quiet status line: live running count, current selection, host + clock,
        # and the back-to-menu hint (a tmux binding that works from the claude
        # pane, so it belongs here rather than in the Textual footer).
        yield Static(id="statusline")

    def on_mount(self) -> None:
        # Enverus-green dark theme drives every stock widget (header, footer,
        # modals, command palette, keys panel) in one move.
        self.register_theme(CLAUDE_MUX_THEME)
        self.theme = "claude-mux"
        self._tree = self.query_one(Tree)
        self._tree.root.set_label(Text.assemble(("◈ ", _GREEN), ("Projects", f"bold {_MUTED}")))
        self._tree.root.expand()
        # Keep the tree the focused widget (its cursor row highlights and drives
        # NodeHighlighted); stop the scroll panel from grabbing Tab focus. vim
        # nav is unaffected regardless — those bindings act on self._tree directly.
        self._tree.focus()
        try:
            self.query_one("#detail", VerticalScroll).can_focus = False
        except Exception:
            pass
        self.sub_title = "popup" if self.popup else "dashboard"
        self._refresh_statusline()
        self._refresh_detail()
        # First paint immediately, then hybrid refresh: slow safety poll + a
        # fast Events File watcher for near-instant `waiting` transitions.
        self._trigger_refresh()
        self.set_interval(4.0, self._trigger_refresh)
        self.set_interval(1.0, self._watch_events)

    # -- refresh (off the UI thread) --------------------------------------- #

    def _trigger_refresh(self) -> None:
        self._refresh_worker()

    @work(exclusive=True, thread=True, group="refresh")
    def _refresh_worker(self) -> None:
        # Capture the generation BEFORE reading the engine: this ordering means
        # a concurrent engine swap can only ever make our result look stale (and
        # be dropped), never make a stale snapshot look current.
        gen = self._refresh_gen
        engine = self.engine
        try:
            projects = engine.snapshot()
        except Exception as exc:  # engine failure must not kill the UI
            self.call_from_thread(
                self.notify, f"refresh failed: {exc}", severity="warning"
            )
            return
        self.call_from_thread(self._rebuild_tree, projects, gen)

    def _watch_events(self) -> None:
        """Cheap tick: keep the clock live, refresh only when Events File changed."""
        self._refresh_statusline()
        try:
            path = panemap.events_path()
            mtime = path.stat().st_mtime if path.exists() else 0.0
        except Exception:
            return
        if mtime > self._events_mtime:
            self._events_mtime = mtime
            self._trigger_refresh()

    def action_refresh(self) -> None:
        self._trigger_refresh()

    # -- tree rendering ----------------------------------------------------- #

    def _rebuild_tree(self, projects: list[Project], gen: int | None = None) -> None:
        # Drop a stale snapshot: if a config edit swapped the engine after this
        # worker started, its generation no longer matches and repainting it
        # would flash the OLD project list back into the tree.
        if gen is not None and gen != self._refresh_gen:
            return
        self._projects = projects
        tree = self._tree
        if tree is None:
            return

        # Preserve the selection across the rebuild — either a Worktree leaf (by
        # path) or a Project node (by root path), so an auto-refresh never yanks
        # the cursor back to the root while the operator is on a project row.
        # Both keys are unique across projects (two repos may share a basename),
        # so the cursor is restored to the exact row it was on.
        selected_path: Path | None = None
        selected_project: Path | None = None
        node = tree.cursor_node
        if node is not None:
            if isinstance(node.data, Worktree):
                selected_path = node.data.path
            elif isinstance(node.data, Project):
                selected_project = node.data.root

        # Preserve each project's expand/collapse state across the rebuild, so a
        # background refresh never re-expands a project the operator collapsed.
        # The authoritative source is self._collapsed_projects (persisted to
        # disk and kept current by the collapse/expand handlers); a Project
        # absent from it defaults to expanded.
        tree.clear()
        tree.root.expand()
        restore = None
        for project in projects:
            expand = project.root not in self._collapsed_projects
            pnode = tree.root.add(project_rich_label(project), data=project, expand=expand)
            if selected_project is not None and project.root == selected_project:
                restore = pnode
            for wt in project.worktrees:
                wnode = pnode.add_leaf(worktree_rich_label(wt), data=wt)
                if selected_path is not None and wt.path == selected_path:
                    restore = wnode

        self._refresh_statusline()
        self._refresh_detail()

        if restore is not None:
            # Defer the cursor restore: freshly-added nodes have no computed
            # line number until the tree re-renders, so moving the cursor now
            # would reset it to the root (line 0). call_after_refresh runs once
            # layout has assigned line numbers.
            def _restore(node=restore) -> None:
                try:
                    tree.move_cursor(node)
                except Exception:
                    pass
                # Reflect the restored selection in the panel (move_cursor's
                # NodeHighlighted also fires this, but be explicit for the case
                # where the cursor was already on the target line).
                self._refresh_detail()

            self.call_after_refresh(_restore)

    # -- status line -------------------------------------------------------- #

    def _status_text(self) -> Text:
        """The quiet status line: running count · selection · host · clock.

        This is the design's replacement for the loud full-width green tmux bar —
        the same facts, carried by one accent and hairline separators.
        """
        running = sum(
            1
            for project in self._projects
            for wt in project.worktrees
            if wt.lifecycle is Lifecycle.LIVE
            and wt.live is not None
            and wt.live.activity is Activity.RUNNING
        )
        text = Text(no_wrap=True)
        text.append("● ", style=_GREEN if running else _FAINT)
        text.append(
            f"{running} running", style=_GREEN if running else _DIM
        )

        wt = self._current_worktree()
        project = self._current_project()
        if wt is not None and project is not None:
            text.append("  ·  ", style=_SEP)
            text.append(f"{project.name}/{wt.branch or wt.path.name}", style=_MUTED)
        elif project is not None:
            text.append("  ·  ", style=_SEP)
            text.append(project.name, style=_MUTED)

        text.append("  ·  ", style=_SEP)
        text.append("prefix + m", style=_TEXT)
        text.append(" returns here", style=_DIM)

        # Always advertise the command palette near the right end of the bar; it
        # is the discoverability entry point for every action beyond the footer.
        text.append("  ·  ", style=_SEP)
        text.append("ctrl+p", style=_TEXT)
        text.append(" palette", style=_DIM)

        text.append("  ·  ", style=_SEP)
        text.append(self._host, style=_MUTED)
        text.append("  ·  ", style=_SEP)
        text.append(datetime.now().strftime("%H:%M"), style=_DIM)
        return text

    def _refresh_statusline(self) -> None:
        try:
            self.query_one("#statusline", Static).update(self._status_text())
        except Exception:
            pass  # status line is decorative; never let it disrupt the UI

    # -- detail panel ------------------------------------------------------- #

    def _refresh_detail(self) -> None:
        """Repaint the side panel from the current cursor selection.

        A Worktree row shows every agent (worktree_detail); a Project node shows a
        rollup (project_detail); anything else shows a placeholder. Decorative, so
        any failure is swallowed rather than allowed to disrupt the UI.
        """
        try:
            body = self.query_one("#detail-body", Static)
        except Exception:
            return
        node = self._tree.cursor_node if self._tree is not None else None
        data = node.data if node is not None else None
        if isinstance(data, Worktree):
            renderable = worktree_detail(data)
        elif isinstance(data, Project):
            renderable = project_detail(data)
        else:
            renderable = Text("Select a worktree", style=_DIM)
        # Stash the raw renderable (the Static wraps it internally, which is awkward
        # to read back) so it is directly inspectable in tests.
        self._detail_renderable = renderable
        body.update(renderable)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        # Keep the "current selection" segment and detail panel in step with the cursor.
        self._refresh_statusline()
        self._refresh_detail()

    # -- selection helpers -------------------------------------------------- #

    def _current_worktree(self) -> Optional[Worktree]:
        tree = self._tree
        if tree is None:
            return None
        node = tree.cursor_node
        if node is not None and isinstance(node.data, Worktree):
            return node.data
        return None

    def _current_project(self) -> Optional[Project]:
        tree = self._tree
        if tree is None:
            return None
        node = tree.cursor_node
        if node is None:
            return None
        if isinstance(node.data, Project):
            return node.data
        if isinstance(node.data, Worktree):
            return self._project_for(node.data)
        return None

    def _project_for(self, wt: Worktree) -> Optional[Project]:
        # Match by object identity, not by name: the worktree stored on a tree
        # node is the exact instance held in its parent Project's ``worktrees``
        # list, so identity finds the true parent even when two configured repos
        # share a directory basename (and thus a Project name). A name match
        # would return the FIRST same-named project and, on remove, delete the
        # wrong config entry.
        for project in self._projects:
            if any(w is wt for w in project.worktrees):
                return project
        # Fallback for any caller passing a reconstructed worktree.
        for project in self._projects:
            if project.name == wt.project_name:
                return project
        return None

    # -- actions ------------------------------------------------------------ #

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        # Enter on a worktree leaf jumps/activates; project nodes just toggle.
        data = event.node.data
        if isinstance(data, Worktree):
            self._jump_or_activate(data)

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        self._record_collapse(event.node, collapsed=True)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        self._record_collapse(event.node, collapsed=False)

    def _record_collapse(self, node, collapsed: bool) -> None:
        """Track a Project's collapse toggle and persist only real changes.

        Fires for every collapse path (Enter, h/l, mouse). Non-Project nodes
        (the root, Worktree leaves) are ignored. Persisting only when the set
        actually changes means the events emitted while _rebuild_tree re-adds
        nodes at their already-known state cause no write, so the 4s refresh
        never touches disk — only a genuine operator toggle does.
        """
        data = node.data
        if not isinstance(data, Project):
            return
        root = data.root
        was_collapsed = root in self._collapsed_projects
        if collapsed:
            self._collapsed_projects.add(root)
        else:
            self._collapsed_projects.discard(root)
        if (root in self._collapsed_projects) == was_collapsed:
            return  # no state change -> nothing to persist
        try:
            treestate.save_collapsed(self._collapsed_projects)
        except OSError as exc:
            # View state is disposable; a failed write must not disrupt the UI.
            self.notify(f"could not save tree state: {exc}", severity="warning")

    # -- vim LEVEL navigation ----------------------------------------------- #

    @staticmethod
    def _sibling_index(node) -> Optional[tuple[list, int]]:
        """Return ``(siblings, index)`` for ``node`` among its parent's children.

        None when the node has no parent (the root) or is somehow not found in
        its parent — callers treat that as 'nowhere to step'.
        """
        parent = node.parent
        if parent is None:
            return None
        siblings = list(parent.children)
        for i, sib in enumerate(siblings):
            if sib is node:
                return siblings, i
        return None

    def action_cursor_down(self) -> None:
        """``j``: move to the next sibling at the current level.

        Next Project on a Project row, next Worktree (of the same Project) on a
        Worktree row. From the container root it steps onto the first Project so
        the tree is reachable on the very first keypress.
        """
        tree = self._tree
        if tree is None:
            return
        node = tree.cursor_node
        if node is None:
            return
        if node is tree.root:
            children = list(node.children)
            if children:
                tree.move_cursor(children[0])
            return
        info = self._sibling_index(node)
        if info is None:
            return
        siblings, idx = info
        if idx + 1 < len(siblings):
            tree.move_cursor(siblings[idx + 1])

    def action_cursor_up(self) -> None:
        """``k``: move to the previous sibling at the current level."""
        tree = self._tree
        if tree is None:
            return
        node = tree.cursor_node
        if node is None or node is tree.root:
            return
        info = self._sibling_index(node)
        if info is None:
            return
        siblings, idx = info
        if idx - 1 >= 0:
            tree.move_cursor(siblings[idx - 1])

    def action_step_in(self) -> None:
        """``l``: step IN a level.

        On a Project: expand it if needed and move onto its first Worktree. On a
        Worktree: resume that Worktree's session (same as Enter). From the root:
        drop onto the first Project.
        """
        tree = self._tree
        if tree is None:
            return
        node = tree.cursor_node
        if node is None:
            return
        if isinstance(node.data, Worktree):
            self._jump_or_activate(node.data)
            return
        # Project row (or the container root): descend to the first child.
        if node.allow_expand and not node.is_expanded:
            node.expand()
        children = list(node.children)
        if children:
            tree.move_cursor(children[0])

    def action_step_out(self) -> None:
        """``h``: step OUT a level.

        From a Worktree back to its parent Project; on a top-level Project (whose
        parent is the container root) collapse it for a tidy view.
        """
        tree = self._tree
        if tree is None:
            return
        node = tree.cursor_node
        if node is None:
            return
        parent = node.parent
        if parent is not None and parent is not tree.root:
            tree.move_cursor(parent)
        elif node.allow_expand and node.is_expanded:
            node.collapse()

    def action_open_editor(self) -> None:
        """``o``: open the selected Worktree in the configured editor."""
        wt = self._current_worktree()
        if wt is None:
            self.notify("Select a worktree to open in the editor", severity="warning")
            return
        self._do_open_editor(wt)

    def action_jump(self) -> None:
        self._jump_or_activate(self._current_worktree())

    def _jump_or_activate(self, wt: Optional[Worktree]) -> None:
        # M9 (ADR-0005): entering a Worktree is one primitive —
        # open_or_select_workspace create-or-selects its window in the owned
        # session. It is safe for every lifecycle: a DORMANT worktree gets a fresh
        # Workspace, an OPEN/LIVE one is simply selected (no duplicate window).
        if wt is None:
            return
        self._do_open_workspace(wt, resume=True)

    def action_resume(self) -> None:
        wt = self._current_worktree()
        if wt is None:
            return
        # Same primitive; resume=True launches ``claude --resume`` when creating a
        # fresh Workspace, and just selects an existing one.
        self._do_open_workspace(wt, resume=True)

    def action_new(self) -> None:
        project = self._current_project()
        if project is None:
            self.notify("Select a project or worktree first", severity="warning")
            return

        try:
            branches = git.list_branches(project.root)
            default_base = git.default_base_branch(project.root, self.config.base_branch)
        except Exception as exc:
            self.notify(f"could not read branches: {exc}", severity="error")
            branches, default_base = [], self.config.base_branch

        def on_branch(result: Optional[tuple[str, str]]) -> None:
            if result:
                branch, base = result
                self._do_new_worktree(project, branch, base)

        self.push_screen(BranchPromptScreen(branches, default_base), on_branch)

    def action_close(self) -> None:
        wt = self._current_worktree()
        if wt is None:
            return
        if wt.lifecycle is Lifecycle.DORMANT:
            self.notify("No workspace to close", severity="warning")
            return

        def on_confirm(ok: bool) -> None:
            if ok:
                self._do_close(wt)

        self.push_screen(
            ConfirmScreen(f"Close the workspace for '{wt.branch}'? (git untouched)"),
            on_confirm,
        )

    # -- project management (edits config.toml only; never the filesystem) -- #

    def _default_pick_start(self) -> Path:
        """Sensible starting directory for the picker: parent of the first
        configured project, else the home directory."""
        if self.config.projects:
            return self.config.projects[0].expanduser().parent
        return Path.home()

    def action_add_project(self) -> None:
        # yazi picker (suspends the TUI while the operator browses) when
        # available; otherwise a plain text-input fallback.
        if picker.yazi_available():
            path = picker.pick_directory(self, start=self._default_pick_start())
            if path is not None:
                self._commit_add_project(path)
            return

        def on_path(value: Optional[str]) -> None:
            if value:
                self._commit_add_project(Path(value))

        self.push_screen(PathPromptScreen(), on_path)

    @work(thread=True, group="config")
    def _commit_add_project(self, path: Path) -> None:
        # Off the UI thread: add_project shells out to ``git rev-parse`` and
        # writes config.toml (fsync). Both can block on a slow/NFS filesystem, so
        # they must never run on the event loop. A write failure (read-only fs,
        # disk full) is caught and surfaced instead of crashing the TUI.
        # Serialized against the remove worker so the two never interleave their
        # load->modify->save of config.toml (which would lose an update).
        with self._config_lock:
            try:
                added, message = add_project(path)
            except Exception as exc:
                self.call_from_thread(
                    self.notify, f"add project failed: {exc}", severity="error"
                )
                return
            self.call_from_thread(
                self.notify, message, severity="information" if added else "warning"
            )
            if added:
                self._reload_config()

    def action_remove_project(self) -> None:
        project = self._current_project()
        if project is None:
            self.notify("Select a project to remove", severity="warning")
            return

        def on_confirm(ok: bool) -> None:
            if ok:
                self._commit_remove_project(project)

        self.push_screen(
            ConfirmScreen(
                f"Remove project '{project.name}' from claude-mux? "
                "(the folder is NOT deleted)"
            ),
            on_confirm,
        )

    @work(thread=True, group="config")
    def _commit_remove_project(self, project: Project) -> None:
        # Off the UI thread: remove_project reads and rewrites config.toml
        # (fsync). A write failure is surfaced rather than crashing the TUI.
        # Serialized against the add worker so the two never interleave their
        # load->modify->save of config.toml (which would lose an update).
        with self._config_lock:
            try:
                removed = remove_project(project.root)
            except Exception as exc:
                self.call_from_thread(
                    self.notify, f"remove project failed: {exc}", severity="error"
                )
                return
            if removed:
                self.call_from_thread(self.notify, f"Removed project: {project.name}")
                self._reload_config()
            else:
                self.call_from_thread(
                    self.notify,
                    f"Project not found in config: {project.name}",
                    severity="warning",
                )

    def _reload_config(self) -> None:
        """Reload config from disk after a config edit (runs on a config worker
        thread). The blocking TOML read happens here; the engine swap and tree
        refresh are marshalled back to the UI thread so the generation bump and
        engine reassignment stay atomic w.r.t. the refresh worker."""
        config = load_config()
        self.call_from_thread(self._apply_config, config)

    def _apply_config(self, config: Config) -> None:
        """UI thread: adopt the reloaded config, bumping the refresh generation
        so any snapshot already in flight against the old engine is discarded."""
        self.config = config
        self.engine = StatusEngine(config)
        self._refresh_gen += 1
        self._trigger_refresh()

    # -- worker-thread lifecycle operations --------------------------------- #

    @work(thread=True, group="lifecycle")
    def _do_open_workspace(self, wt: Worktree, resume: bool = True) -> None:
        # M9 (ADR-0005): create-or-select the Worktree's window in the owned
        # ``claude-mux`` session and select-window (full-screen) + select-pane the
        # claude pane. No switch-client. Resolve the owning Project for the
        # ``<project>/<branch>`` window name; fall back to a minimal Project built
        # from the worktree if identity lookup fails.
        try:
            with self._lifecycle_lock:
                project = self._project_for(wt)
                if project is None:
                    project = Project(name=wt.project_name, root=wt.path)
                activate.open_or_select_workspace(project, wt, self.config, resume=resume)
        except Exception as exc:
            self.call_from_thread(self.notify, f"open failed: {exc}", severity="error")
            return
        self._after_navigation()

    @work(thread=True, group="lifecycle")
    def _do_new_worktree(self, project: Project, branch: str, base: str) -> None:
        try:
            with self._lifecycle_lock:
                activate.new_worktree(project, branch, self.config, base=base)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"new worktree failed: {exc}", severity="error"
            )
            return
        self._after_navigation()

    @work(thread=True, group="lifecycle")
    def _do_close(self, wt: Worktree) -> None:
        # Same lock as open/create: a close must not interleave with an in-flight
        # open of the same Worktree (kill racing create), and vice versa.
        try:
            with self._lifecycle_lock:
                activate.close_workspace(wt)
        except Exception as exc:
            self.call_from_thread(self.notify, f"close failed: {exc}", severity="error")
            return
        self.call_from_thread(self._trigger_refresh)

    @work(thread=True, group="editor")
    def _do_open_editor(self, wt: Worktree) -> None:
        # Off the UI thread: spawn the editor detached (Popen, not run) so a
        # slow-launching GUI never blocks the event loop. ``editor_cmd`` is split
        # with shlex and the worktree path appended — ``code /path/to/wt``. A
        # missing binary (editor not installed / not on PATH) is surfaced as a
        # notification rather than crashing the TUI.
        cmd = shlex.split(self.config.editor_cmd) + [str(wt.path)]
        try:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"open in editor failed: {exc}", severity="error"
            )
            return
        self.call_from_thread(
            self.notify, f"Opening {wt.branch or wt.path.name} in editor"
        )

    def _after_navigation(self) -> None:
        """After a jump/activate: close the popup, else refresh the dashboard."""
        if self.popup:
            self.call_from_thread(self.exit)
        else:
            self.call_from_thread(self._trigger_refresh)


def run_dashboard(config: Config | None = None, popup: bool = False) -> None:
    """Run the Textual dashboard (persistent window, or popup mode if popup=True)."""
    if config is None:
        from claude_mux.config import load_config

        config = load_config()
    app = ClaudeMuxApp(config, popup=popup)
    app.run()
