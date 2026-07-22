# claude-mux owns one tmux session; workspaces are full-screen windows within it

## Status

accepted (supersedes ADR-0002)

## Context & decision

ADR-0002 put each Project in its own tmux session and "jumped" by switching the operator's tmux client to those external sessions. In practice the operator does not want claude-mux reaching out and driving other terminals; they want **everything encapsulated in claude-mux** (the Claude Squad model): a menu to pick a Project/Worktree, and entering one shows *that* Worktree's full-screen **Workspace** — agent panes on the left, `yazi` (top) and a terminal (bottom) stacked in the right column — with the menu one keystroke away. Nothing outside claude-mux is touched.

Decision: **claude-mux owns a single dedicated tmux session** (named `claude-mux`). Its **window 0 is the "menu"** — the Textual Project→Worktree tree. Each entered Worktree is a **window in that same session** carrying the Workspace layout. **Enter/Resume** creates-or-selects that window and `select-window`s to it (full-screen), then selects the `claude` pane. A **session-scoped tmux key binding** returns to the menu window. There is **no `switch-client` to external or per-project sessions**; the Project→Worktree grouping now lives only in the menu tree, not in tmux structure.

## Considered options

- **Persistent menu beside the workspace** (menu pane + workspace panes in one window): matches "slide out from the right" literally, but requires live `join-pane`/`break-pane` juggling to swap Workspaces beside the menu, and steals width from an already pane-dense layout. Rejected for v1.
- **Keep ADR-0002** (per-project sessions + external `switch-client`): rejected — it is exactly the "controlling other terminals" the operator does not want.

## Consequences

- **Launch bootstraps the session.** `claude-mux` ensures the `claude-mux` session exists with the menu in window 0 and places the operator in it — `attach` if launched outside tmux, `switch-client` into it if launched inside tmux.
- **Navigation is `select-window` within the owned session** (+ `select-pane` to `claude`). The ADR-0002/M8 cross-session `switch-client` path is retired for navigation (the `_current_client` helper may remain for the one-time launch switch).
- **Grouping is UI-only.** Workspace windows are named `<project>/<branch>` for legibility.
- **Back-to-menu is a tmux binding** (discoverable, configurable) so it works while focus is in the `claude` pane, not the Textual app.
- **Close Workspace = `kill-window`**; the menu (window 0) always survives. Closing every Workspace leaves just the menu.
- **Persistence** still comes from tmux: detaching leaves the whole claude-mux session (menu + all Workspaces) alive.
