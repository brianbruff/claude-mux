"""Textual dashboard UI: a collapsible Tree of Projects and Worktrees.

The tree's backbone is Projects -> Worktrees (CONTEXT.md). A Live Claude is a
status attribute of a Worktree row, not a node of its own. Rows show lifecycle,
activity, the Session summary, and any Footer Scrape extras that were captured.

Refresh is event-driven with a slow safety poll (ADR-0001/0004): a ~4s interval
runs ``StatusEngine.snapshot`` off the UI thread, and a faster ~1s tick watches
the Events File mtime so a ``waiting`` transition surfaces almost immediately.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, Tree

from claude_mux import activate, panemap
from claude_mux.config import Config
from claude_mux.model import Activity, Lifecycle, LiveClaude, Project, Worktree
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


def activity_text(live: Optional[LiveClaude]) -> str:
    """Short activity descriptor for a Live Claude (empty if none/unknown)."""
    if live is None:
        return ""
    if live.activity is Activity.IDLE:
        return format_idle(live.idle_seconds) or "idle"
    return _ACTIVITY_LABEL.get(live.activity, "")


def scrape_extras(live: Optional[LiveClaude]) -> list[str]:
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
    """Best available 'what is this Claude doing' text for a worktree."""
    if wt.live is not None and wt.live.summary:
        return wt.live.summary
    if wt.latest_session is not None and wt.latest_session.summary:
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
# Modal dialogs                                                                #
# --------------------------------------------------------------------------- #


class BranchPromptScreen(ModalScreen[Optional[str]]):
    """Prompt for a new worktree branch name; dismisses with the name or None."""

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
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("New worktree branch name:")
            yield Input(placeholder="feature/my-branch", id="branch")

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
    Tree {
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("enter", "jump", "Jump / Activate"),
        Binding("n", "new", "New worktree"),
        Binding("r", "resume", "Resume"),
        Binding("x", "close", "Close workspace"),
        Binding("g", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, popup: bool = False):
        """Initialize the app with configuration and popup mode flag."""
        super().__init__()
        self.config = config
        self.popup = popup
        self.engine = StatusEngine(config)
        self._projects: list[Project] = []
        self._events_mtime: float = 0.0
        # NB: not ``self.tree`` — Textual's App defines a read-only ``tree``
        # property (the DOM debug tree), so we hold the widget under ``_tree``.
        self._tree: Tree | None = None

    # -- composition -------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree("Projects", id="tree")
        yield Footer()

    def on_mount(self) -> None:
        self._tree = self.query_one(Tree)
        self._tree.root.expand()
        self.sub_title = "popup" if self.popup else "dashboard"
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
        try:
            projects = self.engine.snapshot()
        except Exception as exc:  # engine failure must not kill the UI
            self.call_from_thread(
                self.notify, f"refresh failed: {exc}", severity="warning"
            )
            return
        self.call_from_thread(self._rebuild_tree, projects)

    def _watch_events(self) -> None:
        """Cheap tick: refresh only when the Events File has changed."""
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

    def _rebuild_tree(self, projects: list[Project]) -> None:
        self._projects = projects
        tree = self._tree
        if tree is None:
            return

        # Preserve the selection across the rebuild — either a Worktree leaf (by
        # path) or a Project node (by name), so an auto-refresh never yanks the
        # cursor back to the root while the operator is on a project row.
        selected_path: Path | None = None
        selected_project: str | None = None
        node = tree.cursor_node
        if node is not None:
            if isinstance(node.data, Worktree):
                selected_path = node.data.path
            elif isinstance(node.data, Project):
                selected_project = node.data.name

        tree.clear()
        tree.root.expand()
        restore = None
        for project in projects:
            pnode = tree.root.add(project_label(project), data=project, expand=True)
            if selected_project is not None and project.name == selected_project:
                restore = pnode
            for wt in project.worktrees:
                wnode = pnode.add_leaf(worktree_label(wt), data=wt)
                if selected_path is not None and wt.path == selected_path:
                    restore = wnode

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

            self.call_after_refresh(_restore)

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

    def action_jump(self) -> None:
        self._jump_or_activate(self._current_worktree())

    def _jump_or_activate(self, wt: Optional[Worktree]) -> None:
        if wt is None:
            return
        if wt.lifecycle is Lifecycle.DORMANT:
            self._do_activate(wt, resume=True)
        else:
            self._do_jump(wt)

    def action_resume(self) -> None:
        wt = self._current_worktree()
        if wt is None:
            return
        # Only a DORMANT worktree needs activating (build Workspace + launch claude
        # --resume). A worktree that already has a Workspace (OPEN/LIVE) must be
        # jumped to, not re-activated — activating again would spawn a second,
        # identically-named window and a duplicate claude attached to the same
        # session. Mirrors the guard in _jump_or_activate.
        if wt.lifecycle is Lifecycle.DORMANT:
            self._do_activate(wt, resume=True)
        else:
            self._do_jump(wt)

    def action_new(self) -> None:
        project = self._current_project()
        if project is None:
            self.notify("Select a project or worktree first", severity="warning")
            return

        def on_branch(branch: Optional[str]) -> None:
            if branch:
                self._do_new_worktree(project, branch)

        self.push_screen(BranchPromptScreen(), on_branch)

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

    # -- worker-thread lifecycle operations --------------------------------- #

    @work(thread=True, group="lifecycle")
    def _do_jump(self, wt: Worktree) -> None:
        try:
            if wt.live is not None:
                tmux.jump_to(wt.live.session_name, pane_id=wt.live.pane_id)
            else:
                project = self._project_for(wt)
                session = (
                    (project.session_name or project.name)
                    if project is not None
                    else wt.project_name
                )
                # An OPEN worktree has a Workspace window but no running claude, so
                # there is no pane to target. Switching Worktrees is a window switch
                # (ADR-0002): select that worktree's window, session-qualified so the
                # bare branch name never resolves against another project's session.
                window = activate._window_name(wt)
                tmux.jump_to(session, window_target=f"{session}:{window}")
        except Exception as exc:
            self.call_from_thread(self.notify, f"jump failed: {exc}", severity="error")
            return
        self._after_navigation()

    @work(thread=True, group="lifecycle")
    def _do_activate(self, wt: Worktree, resume: bool = True) -> None:
        try:
            activate.activate(wt, self.config, resume=resume)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"activate failed: {exc}", severity="error"
            )
            return
        self._after_navigation()

    @work(thread=True, group="lifecycle")
    def _do_new_worktree(self, project: Project, branch: str) -> None:
        try:
            activate.new_worktree(project, branch, self.config)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"new worktree failed: {exc}", severity="error"
            )
            return
        self._after_navigation()

    @work(thread=True, group="lifecycle")
    def _do_close(self, wt: Worktree) -> None:
        try:
            activate.close_workspace(wt)
        except Exception as exc:
            self.call_from_thread(self.notify, f"close failed: {exc}", severity="error")
            return
        self.call_from_thread(self._trigger_refresh)

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
