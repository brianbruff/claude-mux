# Hybrid status source: transcript/process authoritative, capture-pane for extras

## Status

accepted

## Context & decision

claude-mux must show what each **Live Claude** is doing — running vs waiting-on-operator vs idle — plus niceties like context %, cost, and model. Two signals are available: the on-disk `.jsonl` transcript + OS process/pane liveness (stable but coarse), and screen-scraping the pane via `tmux capture-pane` (rich but coupled to Claude Code's TUI layout).

We use **both, in a fixed hierarchy**: the transcript's last-event/mtime plus pane-process liveness are *authoritative* for running-vs-idle and for identity (which conversation this is), and are the only source we depend on for correctness. **Footer Scrape** is layered on top to add context %, cost, elapsed, and `waiting`-for-permission detection, and must **degrade gracefully** — if parsing fails, the row still renders from the core signal.

## Considered options

- **Pure capture-pane scrape** — richest single source, but brittle: multiple Claude Code versions run concurrently (observed 2.1.197/198/199 side by side), and the footer format is not a stable contract. A layout change silently breaks status for every session.
- **Pure jsonl + process** — stable, but can't cleanly distinguish "waiting for a permission prompt" from "idle", and exposes no live cost/context figures.

## Consequences

- Scrape parsing lives behind a single adapter with a version-tolerant parser; a parse miss is a non-event, never a crash or a blank row.
- Correctness never depends on the scrape, so a future Claude Code TUI change degrades features, not reliability.
