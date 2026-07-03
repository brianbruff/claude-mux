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

- **One tmux session per Project, one window (Workspace) per Worktree** — tmux is
  the persistence layer. A Workspace lays out `claude` in a left split, `yazi`
  top-right, and a plain shell bottom-right.
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
uv run claude-mux            # persistent dashboard window (default)
uv run claude-mux dashboard  # same as above
uv run claude-mux popup      # popup mode: jumping to a target closes the dashboard
```

`popup` mode is meant to be bound to a tmux key via `display-popup`, so you can
pop the dashboard from anywhere, jump, and have it dismiss itself.

## Key bindings

| Key     | Action                                                               |
| ------- | -------------------------------------------------------------------- |
| `enter` | Jump to a Live/Open worktree, or **Activate** a Dormant one          |
| `n`     | New worktree in the selected project (prompts for a branch name)     |
| `r`     | Resume — activate the selected worktree, resuming its latest session |
| `x`     | Close the workspace (kills the tmux window only; git is untouched)   |
| `g`     | Refresh now                                                          |
| `q`     | Quit                                                                 |

## Development

```bash
uv sync
uv run pytest -q          # run the test suite
```

The domain model in `src/claude_mux/model.py` is the shared contract every other
module codes against; the module interfaces are specified in
[docs/contract/interfaces.md](docs/contract/interfaces.md).
