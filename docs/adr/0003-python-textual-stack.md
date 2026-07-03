# Build claude-mux in Python + Textual

## Status

accepted

## Context & decision

claude-mux is a TUI that polls tmux, scrapes panes, reads Claude Code's on-disk session metadata, and drives tmux to create/close Workspaces. We build it in **Python + Textual**, managed with `uv`, using `libtmux` to talk to tmux. Textual gives batteries-included list/table widgets, a reactive refresh loop, and async workers for polling — which gets us to a working, iterating tool fastest. Shelling out to git/tmux and parsing JSON is trivial in Python.

## Considered options

- **Rust + ratatui** — near-instant startup (nice for the `display-popup` entry mode) and a single static binary, but a hand-rolled event loop and list/table state mean materially slower time-to-v1.
- **Node + Ink** — comfortable if thinking in React, but fewer batteries-included TUI widgets than Textual.

## Consequences

- Python startup (~150ms) is a slight tax on the popup entry mode; acceptable for v1. If the popup ever feels sluggish, that — not any architectural limit — is the reason to revisit, and this ADR would be superseded rather than the design reworked.
- Runtime is managed with `uv` (per repo convention); no `pip`/`poetry`.
