# claude-mux — Implementation Plan

A TUI for observing and managing Claude Code work across tmux. See [CONTEXT.md](../CONTEXT.md) for terminology and `docs/adr/` for the decisions behind this plan.

## Guiding decisions (from ADRs)

- **Python + Textual**, managed with `uv`, driving tmux via `libtmux`. ([ADR-0003](adr/0003-python-textual-stack.md))
- **One dedicated `claude-mux` tmux session; menu is window 0, each Worktree is a full-screen window (Workspace) in it** — navigation is `select-window`, no cross-session `switch-client`; tmux is the persistence layer. ([ADR-0005](adr/0005-encapsulated-single-session.md), superseding [ADR-0002](adr/0002-tmux-session-per-project.md)'s session-per-Project model)
- **Hybrid status**: jsonl/process authoritative, `capture-pane` scrape for extras, degrading gracefully. ([ADR-0001](adr/0001-hybrid-status-source.md))

## Milestones

Each milestone is independently demoable and builds on the previous. Read-only work comes first; mutation last.

### M1 — Read-only spine

Render the Project ▸ Worktree tree with no mutation of any kind.

- Load config (`~/.config/claude-mux/config.toml`): list of Project root dirs.
- For each Project, enumerate Worktrees via `git worktree list --porcelain`.
- Discover Live Claudes from `tmux list-panes -a` (foreground command is `claude` or a semver-shaped version string).
- Match a Live Claude to its Worktree by `pane_current_path`.
- Render as a collapsible Textual tree; Worktrees with no Live Claude still shown.

**Demo:** launch, see every project and worktree, with a live/absent marker per worktree.

### M2 — Status overlay (hook-driven)

Give each Worktree row a real Status, sourced primarily from hook events.

- **Install hooks** (idempotent, into `~/.claude/settings.json`): `SessionStart`/`SessionEnd` maintain the **Pane Map** (`{tmux_pane, sessionId, cwd, ts}`); `Notification`/`Stop` append Status transitions to the **Events File**. (ADR-0004)
- **Watch** the Events File → instant `running`/`waiting`/`idle` transitions.
- **Slow safety poll (~3–5s):** reconcile tmux truth (closed panes, externally-launched claudes not covered by hooks) and refresh **Footer Scrape** extras (model, context %, cost, elapsed) via `tmux capture-pane`; version-tolerant parser behind one adapter, parse miss is a non-event.
- **Fallback identity:** cwd → slug → process-start nearest jsonl `created`, for sessions predating hook install.

**Demo:** a claude asks for permission → its row flips to `waiting` within a blink; rows show model & cost where scrape succeeds; CPU stays near zero when idle.

### M3 — Jump

Make the tree actionable for navigation.

- Dashboard window entry mode (persistent window) and `display-popup` entry mode (bindable key).
- Enter on a row → `switch-client` (Project session) + `select-window` (Workspace); works from both entry modes.

**Demo:** from anywhere, open claude-mux, pick a session, land in its pane.

### M4 — Activate (the shared primitive)

Take a **Dormant** Worktree (on disk, no window — e.g. created by plain `git worktree add`, or a closed Workspace) to **Live**.

- Create the Project's tmux session if absent; add a window (via `libtmux`).
- Build the layout: `claude`/George top-left, `codex` bottom-left, `yazi` top-right, shell bottom-right.
- Launch claude in the first/top-left pane: `--resume <sessionId>` of the worktree's most recent Session (from its Project Slug's Session Index) if one exists, else fresh.
- Jump to it (reuses M3).

**Demo:** select any Dormant worktree in the tree → one keystroke → ready-to-work, Live, focused. Reactivating a worktree that had prior work resumes that conversation.

### M5 — New Worktree

Layer worktree *creation* on top of Activate.

- Prompt for a branch name; sanitize (`/` → `_`).
- `git worktree add <repo>.worktrees/<branch_sanitized>` off current HEAD → produces a Dormant worktree.
- Immediately **Activate** it (M4).

**Demo:** a branch name becomes a ready-to-work Workspace in one flow.

### M6 — Close & remove

Round out lifecycle teardown.

- **Close Workspace:** `kill-window` with confirm → Worktree returns to Dormant. Never touches git.
- **Remove Worktree:** distinct, separately-confirmed destructive action (`git worktree remove`).

**Demo:** close a Workspace without losing the worktree; explicitly remove a worktree.

### M7 — Project management

Manage the **Project** list from the TUI. `config.py` becomes **read-write**: it writes `config.toml` and nothing else — no project directory or git data is ever created, moved, or deleted.

- **Add Project:** pick a folder via `yazi --cwd-file` (fall back to a text prompt if yazi is absent); validate it is a git repo whose top-level is the chosen folder; dedupe against existing entries by resolved path; append and save.
- **Remove Project:** separately-confirmed; removes the entry from `config.toml` only — never deletes the folder or any git data.

**Demo:** press `a`, pick a repo folder in yazi → it appears in the tree; press `d` on a Project, confirm → it drops from the list while the folder stays on disk.

### M9 — Encapsulated single-session (Claude Squad model)

Fold everything into one owned tmux session — **supersedes the ADR-0002/M8 per-project-session, cross-session `switch-client` navigation**. ([ADR-0005](adr/0005-encapsulated-single-session.md))

- **Bootstrap:** `claude-mux` ensures the `claude-mux` session exists with the Menu (Textual tree) in window 0, then places the operator in it — `attach` if launched outside tmux, `switch-client` if inside. A hidden `_menu` subcommand runs the app in-place in window 0.
- **Enter/Resume a Worktree:** create-or-select a full-screen window named `<project>/<branch>` in the `claude-mux` session, building the Workspace layout and launching claude (resume-aware) on first open; then `select-window` + `select-pane` the claude pane. No `switch-client` to external or per-project sessions.
- **Back to menu:** a session-scoped tmux key binding runs `select-window -t claude-mux:menu`, so it works while focus is in the claude pane.
- **Close Workspace:** `kill-window` in the `claude-mux` session; window 0 (menu) always survives.

**Demo:** launch → menu; pick a Worktree → land full-screen in its claude pane; one keystroke back to the menu; close a Workspace and the menu is still there.

## Config (v1 — minimal)

`~/.config/claude-mux/config.toml`:

```toml
projects = [                              # the only required setting
  "~/Data/Repo/github/claude-mux",
  "~/Data/Repo/pr/mu-power-analyst",
]

[defaults]
worktree_pattern = "{repo}.worktrees/{branch}"   # {branch} sanitized: / -> _
base_branch      = "HEAD"                          # new worktrees branch off this
claude_cmd       = "claude"                         # command run in the first/top-left pane
```

Layout (claude/George-TL / codex-BL / yazi-TR / shell-BR, split ratios), keybindings, and model flag are **hardcoded** in v1 — promoted to config only when actually needed.

`config.py` is **read-write** as of M7: the file is hand-editable and also rewritten in place when Projects are added or removed from the TUI (projects list + `[defaults]` table, atomic write). Only `config.toml` is ever touched.

## Deferred / out of scope for v1

- tmux-resurrect / continuum for full reboot survival (conversation-level recovery via stored `sessionId` covers the common case).
- Auto-registering / scanning parent dirs for Projects (config is explicit for now).
- Layout / command / keybinding overrides in config.

## Resolved build details

- **pane → sessionId matching:** authoritative via hook-maintained **Pane Map**, heuristic fallback (ADR-0004). Shared-cwd case handled.
- **Refresh cadence:** hook-event-driven + ~3–5s safety poll (ADR-0004 / M2).
- **Config schema:** minimal, above.
