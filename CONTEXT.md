# claude-mux

A terminal UI for observing and managing Claude Code work happening across tmux. It joins tmux's live process/pane state with Claude Code's on-disk session metadata so the operator can see what every running Claude is doing and jump to it, and can spin up new isolated work areas on demand.

## Language

**Session** _(ambiguous — see Flagged ambiguities)_:
A single Claude Code conversation, identified by a `sessionId` and persisted on disk as a `.jsonl` transcript. Survives the terminal being closed.
_Avoid_ using "session" bare to mean a tmux pane or window.

**Live Claude**:
A tmux pane whose foreground process is `claude`. The primary object in the main list (confirmed). Enriched with metadata (summary, branch) from the matching **Session Index** entry.
_Avoid_: "running session", "active session" (ambiguous with tmux's own "attached session").

**Session Index**:
The `sessions-index.json` file Claude Code maintains per project under `~/.claude/projects/<slug>/`. Provides `summary`, `firstPrompt`, `messageCount`, `modified`, `gitBranch`, `projectPath` for every past conversation. The source of "what is this Claude doing" text.

**Pane Map**:
The authoritative `pane ↔ sessionId` mapping claude-mux relies on to know which **Session** a **Live Claude** pane is running. Populated by a **SessionStart** hook that records `{tmux_pane, sessionId, cwd, timestamp}` on every claude launch. For sessions predating the hook install, a heuristic (cwd → **Project Slug** → process-start-time nearest jsonl `created`) fills the gap. (See ADR-0004.)

**Status**:
The derived activity state of a **Live Claude**: `running`, `waiting` (blocked on the operator — input or a permission prompt), or `idle Nm`. Primary source is **event-driven**: Claude Code `Notification`/`Stop`/`SessionStart`/`SessionEnd` hooks write transitions to a watched **Events File** (instant, accurate `waiting`). A slow safety poll (~3–5s) reconciles tmux truth (closed panes, externally-launched claudes) and refreshes **Footer Scrape** extras (cost, context %). Scrape and process/jsonl signals are the fallback when a hook event is missing.

**Events File**:
The claude-mux-owned file the status hooks append to; claude-mux watches it for near-instant Status transitions. Populated by the same hook install that maintains the **Pane Map**.

**Footer Scrape**:
Reading a claude pane's rendered screen via `tmux capture-pane` and parsing its footer/prompt (model, project, branch, context %, cost, elapsed, prompt state). Best-effort and version-sensitive — never the sole source of truth.

**Project Slug**:
Claude Code's encoding of a project's absolute path into a directory name, replacing `/` with `-` (e.g. `/Users/brian.keating/Data/Repo/github/claude-mux` → `-Users-brian-keating-Data-Repo-github-claude-mux`).

**Project**:
A top-level git repository the operator works in (e.g. `mu-power-analyst`). Has a main working tree and zero or more **Worktrees**. Identity is the repo, not any single path. The set of Projects is the **Config**'s only required setting.

**Config** _(user-editable AND TUI-managed)_:
The `~/.config/claude-mux/config.toml` file listing the operator's **Projects** (plus a few defaults). Hand-editable, but also read-write from the TUI: **Project management** actions add and remove entries in place. claude-mux only ever edits this file — it never creates, moves, or deletes a Project's directory or git data.

**Worktree**:
A git worktree of a **Project**, checked out to its own directory (convention: `<repo>.worktrees/<branch_sanitized>`) on its own branch. Crucially, each Worktree is a *distinct* **Project Slug** to Claude Code and therefore has its own **Session Index**. One Project has many Worktrees.

**Workspace**:
A tmux **window** created by claude-mux for one **Worktree**, with a default layout: `claude` in a left vertical split, **yazi** top-right, a plain terminal bottom-right. A Worktree has *at most one* Workspace, but may have **none** (a Dormant Worktree exists on disk with no window).
_Avoid_: "session" (collides with **Session**), "window" bare (too generic).

**Worktree lifecycle** — a Worktree is in exactly one state:
- **Dormant**: exists on disk (from `git worktree list`), no tmux window.
- **Open**: has a **Workspace** (window + layout) but no `claude` running in it.
- **Live**: has a Workspace with a running Claude (a **Live Claude**).

**Activate**:
The action that takes a **Dormant** Worktree to **Live** (confirmed): build its **Workspace** and auto-launch `claude` in the left pane (`--resume` the most recent **Session** for that worktree's **Project Slug** if one exists, else fresh), then jump to it. Creating a **New Workspace** is "create the worktree, then Activate it" — Activate is the shared primitive.

**tmux mapping**:
One tmux **session per Project**; each **Workspace** (Worktree) is a **window** within that session. tmux's own structure thus mirrors the Project → Worktree tree, and tmux becomes the persistence/grouping layer. (Confirmed. See ADR-0002.)

## Flagged ambiguities

- **"session"** collides across three meanings: (1) a Claude conversation, (2) a live `claude` process in a pane, (3) a tmux session/window. Resolution in progress: reserve **Session** for the Claude conversation, use **Live Claude** for the running process, and **Workspace** for the tmux window container. tmux's own "session" is referred to as "tmux session" explicitly.
- **Spine of the main list** — RESOLVED (refined): the tree's backbone is **Projects → Worktrees** (a collapsible two-level tree). A **Live Claude** is a *status attribute* of a Worktree row, not the backbone itself. Worktrees with no Live Claude are still shown, offering "resume" of their most recent **Session**. (This supersedes the earlier "spine = Live Claudes / dormant in a separate view" framing.)
- **Project management** — RESOLVED: adding a Project picks a folder via `yazi --cwd-file`, validates it is a git repo, dedupes it against existing entries, and appends it to the **Config**; removing a Project is confirm-gated and edits the Config only. Neither action touches the filesystem — removal never deletes the folder.
