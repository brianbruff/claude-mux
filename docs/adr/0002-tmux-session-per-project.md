# One tmux session per Project, one window per Worktree

## Status

accepted

## Context & decision

claude-mux manages many **Projects**, each with many **Worktrees**, and wants to "leverage session persistence." We map the domain onto tmux structurally: **one tmux session per Project**, and each **Worktree** is a **window** (a **Workspace**) within that session. The Project → Worktree tree in the UI is therefore a direct reflection of tmux's own session/window hierarchy, and tmux itself is the persistence and grouping layer — detaching leaves every Workspace alive exactly as it was.

## Considered options

- **Current session, new window** — simplest, but interleaves windows from unrelated projects in one session; grouping would exist only in claude-mux's own model, not in tmux, so detaching/reattaching outside the TUI loses the structure.
- **One dedicated `claude-mux` session for everything** — a single place, but the window list grows unbounded and project grouping is again only logical.

We chose per-Project sessions because the grouping and persistence then come "for free" from tmux, and the same structure is legible whether the operator is inside claude-mux or driving raw tmux.

## Consequences

- Creating a Workspace may need to create the Project's session first (create-if-absent).
- "Jump to" is `switch-client`/`select-window`; switching Projects is a session switch, switching Worktrees is a window switch — this holds both from the dashboard window and from a `display-popup`.
- Full reboot survival (tmux-resurrect/continuum) remains optional and additive; conversation-level recovery is handled separately by storing each Workspace's `sessionId` for `claude --resume`.
