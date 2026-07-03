# Claude Code hooks are the authoritative pane map and status-event source

## Status

accepted

## Context & decision

claude-mux must know which Claude conversation (`sessionId`) a given tmux pane's **Live Claude** is running, so status, resume, and "jump" refer to the right thing. macOS has no `/proc`, so a running claude's environment (`$TMUX_PANE`, session id) cannot be read from the live process. Correlating cwd → **Project Slug** → newest `.jsonl` is ambiguous when two Live Claudes share one cwd.

We make the **Pane Map** authoritative by installing a Claude Code **SessionStart** hook that appends `{tmux_pane, sessionId, cwd, timestamp}` to a claude-mux-owned file on every launch. The hook fires with the session id available and inherits `$TMUX_PANE` from the pane it launched in, giving ground truth for **all** claudes — including ones started outside claude-mux and the shared-cwd case. The cwd/start-time heuristic remains only as a fallback for sessions that predate hook installation.

## Considered options

- **Launch-time capture only** — exact for claudes claude-mux itself launched (Activate), but externally-launched sessions fall back to the fuzzy heuristic, and shared-cwd external claudes can be mislabeled. The tool's premise ("sessions could be on any window") makes external claudes first-class, so this is insufficient.
- **Pure heuristic** — no settings changes, but wrong precisely in the shared-cwd case we care about.

## Related decision — status is event-driven off the same hooks

The same install also registers `Notification`, `Stop`, `SessionStart`, and `SessionEnd` hooks that append Status transitions to a watched **Events File**. This makes `waiting` (claude wants the operator) instant and accurate instead of scrape-inferred, and lets claude-mux idle at near-zero CPU. A slow safety poll (~3–5s) reconciles tmux truth and refreshes scrape-only extras (cost, context %). Rejected alternatives: fixed full poll (wastes cycles, laggy `waiting`) and tiered poll (simpler but no extra hooks — kept as the fallback if the hook contract proves unstable).

## Consequences

- claude-mux modifies the user's `~/.claude/settings.json` to register the hooks (idempotent install; surfaced to the user, not silent).
- Correctness of identity now depends on a Claude Code hook contract; if the hook payload shape changes, the map breaks and we fall back to the heuristic — degraded, not broken (mirrors the ADR-0001 philosophy).
- The Pane Map is append-only and pruned against live panes; stale entries (pane closed) are ignored.
