# claude-mux interface contract

All modules live in `src/claude_mux/` and import domain types from `claude_mux.model`.
Behavior is defined by `CONTEXT.md`, `docs/adr/*.md`, and `docs/plan.md` (authoritative).

**Rules for implementers:** do NOT edit `model.py`. Do NOT edit `pyproject.toml` or run
`uv add`/`uv sync` (note any missing dependency in your return value). You MAY add focused
unit tests under `tests/`.

---

### config.py
```
@dataclass Config:
    projects: list[Path]
    worktree_pattern: str = "{repo}.worktrees/{branch}"
    base_branch: str = "HEAD"
    claude_cmd: str = "claude"

def default_config_path() -> Path       # ~/.config/claude-mux/config.toml
def load_config(path: Path | None = None) -> Config
    # tomllib (stdlib). Expand ~ in project paths. Missing file -> Config([]) with defaults.
```

### sessions.py  (reads ~/.claude, no writes)
```
def claude_projects_dir() -> Path        # ~/.claude/projects
def encode_slug(path: Path) -> str       # '/A/b c' -> '-A-b-c' (claude replaces '/' and non-alnum with '-')
def read_session_index(slug: str) -> list[SessionMeta]  # parse sessions-index.json; [] if absent
def latest_session(slug: str) -> SessionMeta | None     # newest by modified
```
The real index entry has keys: `sessionId, fullPath, fileMtime, firstPrompt, summary,
messageCount, created, modified, gitBranch, projectPath, isSidechain`. `modified` is an
ISO-8601 string; convert to epoch seconds for `SessionMeta.modified`.

### git.py  (subprocess to git)
```
def sanitize_branch(branch: str) -> str  # '/' -> '_'
def list_worktrees(project_root: Path) -> list[Worktree]
    # parse `git worktree list --porcelain`; set is_primary (first/bare), branch, path,
    # slug via sessions.encode_slug, project_name = project_root.name, lifecycle=DORMANT.
def add_worktree(project_root: Path, branch: str, pattern: str, base: str) -> Path
    # git worktree add <pattern-expanded> [-b branch] <base>; returns new worktree path.
    # pattern uses {repo} (project_root.name) and {branch} (sanitized).
def remove_worktree(path: Path) -> None  # git worktree remove
```

### tmux.py  (libtmux; capture-pane via libtmux or subprocess)
```
@dataclass PaneInfo:
    pane_id: str; session_name: str; window_index: int; pane_index: int
    current_command: str; pid: int; current_path: Path

def is_claude_command(cmd: str) -> bool   # 'claude', 'node', or semver-shaped like '2.1.199'
def list_panes() -> list[PaneInfo]        # across all sessions
def capture_pane(pane_id: str, lines: int = 8) -> str
def ensure_session(session_name: str, cwd: Path) -> None       # create-if-absent (detached)
def new_window(session_name: str, window_name: str, cwd: Path) -> str  # returns window target
def build_workspace_layout(session_name: str, window_target: str, cwd: Path, claude_cmd: str) -> dict
    # claude LEFT vertical split (~50%), yazi TOP-RIGHT, shell BOTTOM-RIGHT.
    # returns {'claude': pane_id, 'yazi': pane_id, 'shell': pane_id}
def jump_to(session_name: str, window_target: str | None = None, pane_id: str | None = None) -> None
    # switch-client + select-window (+ select-pane). Must work from inside a display-popup.
def kill_window(session_name: str, window_target: str) -> None
```

### scrape.py  (pure parsing — MUST NEVER raise)
```
@dataclass ScrapeResult:
    model: str|None; project: str|None; branch: str|None
    context_pct: int|None; cost_usd: float|None; elapsed: str|None; waiting: bool|None

def parse_footer(captured: str) -> ScrapeResult
```
Version-tolerant parse of a claude pane's last lines. Reference real footer:
```
[Opus 4.8] 📁 mu-power-analyst | 🌿 main
░░░░░░░░░░ 5% | $1.04 | ⏱️ 2m 59s
⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
```
`waiting=True` if a permission/confirmation prompt is visible (e.g. "Do you want",
a numbered Yes/No menu). Any field not confidently found -> None. Never throw.

### panemap.py  (state files under ~/.claude/claude-mux/)
```
@dataclass PaneMapEntry: tmux_pane: str; session_id: str; cwd: Path; ts: float
@dataclass StatusEvent:  tmux_pane: str; session_id: str|None; kind: str; ts: float
    # kind in {'start','stop','notification','waiting','end'}

def state_dir() -> Path            # ~/.claude/claude-mux (mkdir -p)
def pane_map_path() -> Path        # panes.jsonl
def events_path() -> Path          # events.jsonl
def read_pane_map() -> dict[str, PaneMapEntry]   # keyed by tmux_pane, last-wins
def read_events(since_ts: float = 0.0) -> list[StatusEvent]
def append_pane_map(entry: PaneMapEntry) -> None
def append_event(ev: StatusEvent) -> None
```

### hooks.py  (installs into ~/.claude/settings.json — idempotent + non-destructive)
```
def settings_path() -> Path        # ~/.claude/settings.json
def hook_command() -> str          # the command claude invokes, e.g. 'python -m claude_mux hook <event>'
def install_hooks(settings: Path | None = None) -> list[str]
    # add SessionStart/SessionEnd/Notification/Stop entries; MERGE into existing hooks,
    # never clobber the user's existing hooks; idempotent; return list of changes made.
def uninstall_hooks(settings: Path | None = None) -> list[str]
def run_hook(event_name: str) -> None
    # hook entrypoint: read hook JSON from stdin (session_id, cwd, ...), read $TMUX_PANE from env,
    # append to pane map (on start) and/or events file. NEVER crash claude: swallow all errors.
```

### status.py
```
class StatusEngine:
    def __init__(self, config: Config): ...
    def snapshot(self) -> list[Project]:
        # skeleton: config.projects -> git.list_worktrees
        # overlay:  tmux.list_panes (claude panes) matched to worktrees by current_path
        # identity: panemap.read_pane_map (authoritative) else heuristic (slug newest session)
        # activity: panemap.read_events (latest per pane) primary; scrape.parse_footer fallback/extras;
        #           idle age from jsonl mtime
        # sets Worktree.lifecycle (DORMANT/OPEN/LIVE), .live, .latest_session
    def refresh_scrape(self, live: LiveClaude) -> None   # capture_pane + parse_footer -> fill extras
```

### activate.py  (the lifecycle primitive; Activate = build Workspace + auto-launch claude)
```
def activate(worktree: Worktree, config: Config, resume: bool = True) -> None
    # ensure_session; new_window; build_workspace_layout; launch claude in left pane:
    #   resume and worktree.latest_session -> 'claude --resume <session_id>' else config.claude_cmd; then jump_to.
def new_worktree(project: Project, branch: str, config: Config) -> Worktree
    # git.add_worktree -> Worktree(DORMANT) -> activate(it)
def close_workspace(worktree: Worktree) -> None   # kill_window ONLY; NEVER touch git
def remove_worktree(worktree: Worktree) -> None    # git.remove_worktree; destructive; caller must confirm
```

### app.py + __main__.py  (Textual UI + CLI)
```
# __main__.py: def main() with argparse subcommands:
#   (default / 'dashboard'): run the Textual app as a persistent window
#   'popup': run the app in popup mode (jump closes it)
#   'install-hooks' / 'uninstall-hooks': call hooks.* and print changes
#   'hook <event>': call hooks.run_hook(event)  (the Claude Code hook entrypoint)
# app.py: Textual App with a collapsible Tree (Project -> Worktree rows showing
#   lifecycle + Activity + summary + scrape extras).
#   Bindings: enter = jump (LIVE/OPEN) or activate (DORMANT); n = new worktree (prompt
#   branch); r = resume; x = close workspace (confirm); q = quit.
#   Auto-refresh: StatusEngine.snapshot on ~3-5s interval via a Textual worker (off UI thread);
#   watch events file for faster 'waiting' updates if feasible.
```

---

## Project management (new — M7)

Config becomes READ-WRITE. Adding/removing a project only edits `config.toml`; it
NEVER creates, deletes, or modifies any project directory or git data.

### config.py (additions)
```
def save_config(config: Config, path: Path | None = None) -> None
    # Regenerate config.toml from Config (projects list + [defaults] table). Write a fixed
    # header comment. Create the parent dir if needed. ATOMIC write (temp file + os.replace).
    # Use tomli_w for serialization (add the dependency).
def add_project(project_root: Path, path: Path | None = None) -> tuple[bool, str]
    # expanduser + resolve. Validate it is a git repo whose top-level == project_root
    # (git -C <p> rev-parse --show-toplevel). Dedupe against existing entries by resolved path.
    # Append + save. Return (added, human_message). Filesystem untouched beyond config.toml.
def remove_project(project_root: Path, path: Path | None = None) -> bool
    # Remove the matching entry (compare by resolved path) from config + save. Return True if
    # removed. MUST NOT delete the project directory or any git data.
```

### picker.py (new module)
```
def yazi_available() -> bool                      # shutil.which('yazi') is not None
def pick_directory(app, start: Path | None = None) -> Path | None
    # Suspend the Textual app (with app.suspend():) and run `yazi --cwd-file=<tmp> [start]`
    # in the restored terminal; read the chosen directory from <tmp> on exit.
    # Returns None if cancelled / yazi missing / temp empty. MUST restore the app cleanly
    # even on error (finally: cleanup temp). No exception may escape to crash the TUI.
```

### app.py (additions)
```
# Bindings: 'a' = add project (folder picker); 'd' = remove selected project (confirm).
def action_add_project(self) -> None
    # pick_directory(self) -> add_project(...) -> notify(message) -> refresh.
    # If not yazi_available(): fall back to a text Input prompt for the path.
def action_remove_project(self) -> None
    # Require a selected Project node (_current_project). Push a ConfirmScreen(ModalScreen)
    # 'Remove project <name> from claude-mux? (folder is NOT deleted)'. On confirm ->
    # remove_project(...) -> notify -> refresh. If no project selected: notify a hint, no-op.
```
A small `ConfirmScreen(ModalScreen[bool])` with Yes/No buttons is acceptable.
