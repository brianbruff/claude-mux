"""Textual dashboard UI: a collapsible Tree of Projects and Worktrees.

The tree's backbone is Projects -> Worktrees (CONTEXT.md). A Live Claude is a
status attribute of a Worktree row, not a node of its own. Rows show lifecycle,
activity, the Session summary, and any Footer Scrape extras that were captured.

Refresh is event-driven with a slow safety poll (ADR-0001/0004): a ~4s interval
runs ``StatusEngine.snapshot`` off the UI thread, and a faster ~1s tick watches
the Events File mtime so a ``waiting`` transition surfaces almost immediately.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, Static, Tree

from claude_mux import activate, panemap, picker
from claude_mux.config import Config, add_project, load_config, remove_project
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
        Binding("enter", "jump", "Enter workspace"),
        Binding("n", "new", "New worktree"),
        Binding("r", "resume", "Resume"),
        Binding("x", "close", "Close workspace"),
        Binding("a", "add_project", "Add project"),
        Binding("d", "remove_project", "Remove project"),
        Binding("g", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        # vim-style tree navigation. j/k mirror the arrow keys (kept off the
        # footer to avoid clutter); l/h expand/collapse and are shown. Single
        # letters are safe next to the modal Inputs: an Input consumes printable
        # keys itself, so these never fire while a BranchPrompt/PathPrompt is
        # focused (same as the existing n/r/x/a/d bindings).
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "expand_node", "Expand / in"),
        Binding("h", "collapse_node", "Collapse / out"),
    ]

    def __init__(self, config: Config, popup: bool = False):
        """Initialize the app with configuration and popup mode flag."""
        super().__init__()
        self.config = config
        self.popup = popup
        self.engine = StatusEngine(config)
        self._projects: list[Project] = []
        self._events_mtime: float = 0.0
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

    # -- composition -------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Header()
        yield Tree("Projects", id="tree")
        # Menu footer hint: the back-to-menu tmux binding works while focus is in
        # the claude pane (a tmux binding, not a Textual one), so surface it here.
        yield Static("enter a worktree → full-screen workspace · prefix + m returns here", id="menuhint")
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

        tree.clear()
        tree.root.expand()
        restore = None
        for project in projects:
            pnode = tree.root.add(project_label(project), data=project, expand=True)
            if selected_project is not None and project.root == selected_project:
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

    # -- vim tree navigation ------------------------------------------------ #

    def action_cursor_down(self) -> None:
        if self._tree is not None:
            self._tree.action_cursor_down()

    def action_cursor_up(self) -> None:
        if self._tree is not None:
            self._tree.action_cursor_up()

    def action_expand_node(self) -> None:
        """``l``: expand a collapsed node, else step into its first child."""
        tree = self._tree
        if tree is None:
            return
        node = tree.cursor_node
        if node is None:
            return
        if node.allow_expand and not node.is_expanded:
            node.expand()
        else:
            # Already expanded (or a leaf): step in / down to the next row.
            tree.action_cursor_down()

    def action_collapse_node(self) -> None:
        """``h``: collapse an expanded node, else step out to its parent."""
        tree = self._tree
        if tree is None:
            return
        node = tree.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not tree.root:
            tree.move_cursor(node.parent)

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
    def _do_new_worktree(self, project: Project, branch: str) -> None:
        try:
            with self._lifecycle_lock:
                activate.new_worktree(project, branch, self.config)
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
