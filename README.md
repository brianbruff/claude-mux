# claude-mux

A terminal UI for observing and managing Claude Code work across tmux. It joins
tmux's live pane/process state with Claude Code's on-disk session metadata, so
you can see what every running Claude is doing, jump to it, and spin up isolated
git-worktree workspaces on demand.

The dashboard is a collapsible **Project ▸ Worktree** tree. Each worktree row
shows its lifecycle (`○` dormant / `◐` open / `●` live), the Claude's activity
(`running` / `waiting` / `idle Nm`), the session summary, and — where a footer
scrape succeeds — model, context %, cost, and elapsed time.

See [CONTEXT.md](CONTEXT.md) for the full domain language and
[docs/plan.md](docs/plan.md) for the milestone plan and the decisions behind it.

## How it works

- **One dedicated `claude-mux` tmux session** — the Claude Squad model (ADR-0005).
  Everything lives inside this single session. **Window 0 is the menu** (this
  Project ▸ Worktree tree). Entering a worktree opens or selects a **full-screen
  window** in the same session, named `<project>/<branch>`, laid out as `claude`
  in a left split, `yazi` top-right, and a plain shell bottom-right. tmux is the
  persistence layer: detaching leaves the menu and every workspace alive.
- **Back to the menu is one keystroke: `prefix + m`** (with the default tmux
  prefix, `Ctrl-b m`). It is a tmux binding, so it works even while focus is in
  the `claude` pane or an editor — not just in the menu. It jumps to the menu
  window (`select-window -t claude-mux:menu`). Nothing outside claude-mux is ever
  touched — no `switch-client` to other terminals or sessions.
- **Hybrid status** — Claude Code hooks write pane→session identity and activity
  transitions to state files under `~/.claude/claude-mux/` (near-instant, accurate
  `waiting`). A slow safety poll reconciles tmux truth and scrapes footer extras.
  Every source degrades gracefully: a missing signal never crashes the dashboard.

## Requirements

- Python ≥ 3.11
- `tmux` and (for the default Workspace layout) `yazi` on `PATH`
- `git`
- [`uv`](https://docs.astral.sh/uv/) for install / running

## Install

```bash
uv sync            # install runtime deps into .venv
uv run claude-mux --help
```

## Configure

Create `~/.config/claude-mux/config.toml` listing your project roots. Only
`projects` is required; the `[defaults]` table is optional.

```toml
projects = [                              # the only required setting
  "~/Data/Repo/github/claude-mux",
  "~/Data/Repo/pr/mu-power-analyst",
]

[defaults]
worktree_pattern = "{repo}.worktrees/{branch}"   # {branch} sanitized: / -> _
base_branch      = "HEAD"                          # new worktrees branch off this
claude_cmd       = "claude"                         # command run in the left pane
```

A missing config file is fine — the dashboard just starts with no projects.

## Install the Claude Code hooks

For accurate, near-instant status (and reliable pane→session identity), install
the hooks once. This **merges** into `~/.claude/settings.json` without clobbering
your existing hooks and is idempotent:

```bash
uv run claude-mux install-hooks     # prints the changes it made
uv run claude-mux uninstall-hooks   # cleanly removes only claude-mux's entries
```

The hooks register `SessionStart`, `SessionEnd`, `Notification`, and `Stop`, each
invoking `claude-mux hook <event>` as a subprocess. The hook entrypoint never
raises and never exits non-zero, so it can't disrupt a running Claude.

Without the hooks the dashboard still works, falling back to a cwd→slug heuristic
for identity and footer scraping for activity.

## Run

```bash
uv run claude-mux            # bootstrap the claude-mux session and enter its menu
uv run claude-mux dashboard  # same as above
```

`claude-mux` **bootstraps the dedicated `claude-mux` tmux session** (creating it
with the menu in window 0 if it does not exist) and places you in it: it `exec`s
`tmux attach` when run from a plain terminal, or `switch-client`s into it when run
from inside tmux. Re-running it while you are already in the menu is a safe no-op.

Once you are in the menu:

- **Enter** a worktree → its full-screen workspace (`claude` left, `yazi`
  top-right, a shell bottom-right).
- **`prefix + m`** (e.g. `Ctrl-b m`) → back to the menu, from anywhere, even with
  focus in `claude`.
- Detach (`prefix + d`) to leave everything running; `uv run claude-mux` re-enters.

## Key bindings

In the menu (Textual) tree:

| Key     | Action                                                               |
| ------- | -------------------------------------------------------------------- |
| `enter` | Enter the selected worktree's full-screen workspace (create or select) |
| `n`     | New worktree in the selected project (prompts for a branch name)     |
| `r`     | Resume — enter the worktree, resuming its latest session             |
| `x`     | Close the workspace (kills its tmux window only; git is untouched)   |
| `a`     | Add a project (yazi folder picker, or a path prompt if yazi missing) |
| `d`     | Remove the selected project from claude-mux (the folder is NOT deleted) |
| `o`     | Open the selected worktree in your editor (`editor_cmd`, default `code`) |
| `g`     | Refresh now                                                          |
| `q`     | Quit                                                                 |

Vim-style **level** navigation (the tree has two levels — projects and their worktrees):

| Key     | Action                                                               |
| ------- | -------------------------------------------------------------------- |
| `j` / `k` | Move to the next / previous sibling at the current level (project↔project, or worktree↔worktree of the same project) |
| `l`     | Step **in**: on a project, into its first worktree; on a worktree, resume its session |
| `h`     | Step **out**: from a worktree back to its project (collapses a top-level project) |

Anywhere in the `claude-mux` session (tmux binding, works inside the `claude` pane):

| Key          | Action                                        |
| ------------ | --------------------------------------------- |
| `prefix + m` | Jump back to the menu (window 0)              |

## Development

```bash
uv sync
uv run pytest -q          # run the test suite
```

The domain model in `src/claude_mux/model.py` is the shared contract every other
module codes against; the module interfaces are specified in
[docs/contract/interfaces.md](docs/contract/interfaces.md).
