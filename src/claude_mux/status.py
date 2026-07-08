"""StatusEngine: assemble the dashboard snapshot from git, tmux, pane map, and scrape.

The snapshot follows the hybrid-status hierarchy of ADR-0001: the pane map and
hook events are authoritative for identity and activity; footer scrape only layers
on cost/context/model extras and a ``waiting`` fallback. Every external read is
wrapped so that a failure in one source degrades that row (or project) instead of
crashing the whole snapshot, which feeds a live UI worker.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from claude_mux import git, panemap, scrape, sessions, tmux
from claude_mux.config import Config
from claude_mux.model import (
    Activity,
    AgentKind,
    Lifecycle,
    LiveAgent,
    Project,
    SessionMeta,
    Worktree,
)
from claude_mux.scrape import ScrapeResult
from claude_mux.tmux import PaneInfo

# A Live Claude with no hook event and no scrape ``waiting`` signal is treated as
# IDLE once its transcript has been quiet for at least this long, otherwise RUNNING.
IDLE_THRESHOLD_SECONDS = 60

# Map a hook event kind (panemap.StatusEvent.kind) to a derived Activity.
_EVENT_ACTIVITY: dict[str, Activity] = {
    "start": Activity.RUNNING,
    "stop": Activity.IDLE,
    "end": Activity.IDLE,
    "notification": Activity.WAITING,
    "waiting": Activity.WAITING,
}


def _resolve(path: Path) -> Path:
    """Best-effort absolute/normalized path for comparison; never raises."""
    try:
        return Path(path).expanduser().resolve()
    except Exception:
        return Path(path)


class StatusEngine:
    """Builds Project/Worktree snapshots by overlaying tmux + session state."""

    def __init__(self, config: Config):
        """Initialize the engine with user configuration."""
        self.config = config

    # ------------------------------------------------------------------ public

    def snapshot(self) -> list[Project]:
        """Build the current list of Projects with lifecycle/activity resolved.

        Skeleton comes from ``git.list_worktrees`` per configured project. tmux
        panes overlay Live Claudes (matched by ``current_path``); identity comes
        from the pane map (authoritative) or the newest session for the slug
        (heuristic); activity comes from the latest hook event per pane, with
        footer scrape as the fallback and for extras.
        """
        panes = self._safe(tmux.list_panes, default=[])
        pane_map = self._safe(panemap.read_pane_map, default={})
        latest_events = self._latest_events_by_pane()

        # One process-table snapshot for this build; shared by every _agent_kind
        # call so node-hosted agents are disambiguated without re-running `ps`.
        self._procs = self._safe(tmux.capture_processes, default=None)

        agent_panes = [p for p in panes if self._agent_kind(p) is not None]

        projects: list[Project] = []
        for root in self.config.projects:
            projects.append(
                self._build_project(root, panes, agent_panes, pane_map, latest_events)
            )
        return projects

    def refresh_scrape(self, live: LiveAgent) -> None:
        """Capture the pane and parse the footer to fill scrape extras on a LiveClaude."""
        self._apply_scrape(live)

    # ------------------------------------------------------------- per-project

    def _build_project(
        self,
        root: Path,
        panes: list[PaneInfo],
        agent_panes: list[PaneInfo],
        pane_map: dict,
        latest_events: dict,
    ) -> Project:
        root = _resolve(root)
        name = root.name
        project = Project(name=name, root=root, session_name=name)

        worktrees = self._safe(git.list_worktrees, root, default=[])
        for wt in worktrees:
            try:
                self._resolve_worktree(wt, panes, agent_panes, pane_map, latest_events)
            except Exception:
                # A single worktree failing to resolve must not drop the project.
                pass
        project.worktrees = worktrees
        return project

    def _resolve_worktree(
        self,
        wt: Worktree,
        panes: list[PaneInfo],
        agent_panes: list[PaneInfo],
        pane_map: dict,
        latest_events: dict,
    ) -> None:
        wt_path = _resolve(wt.path)

        # Session Index for this worktree's slug (used for resume + summary +
        # heuristic identity). Compute the newest entry ourselves to avoid a
        # second file read.
        index = self._safe(sessions.read_session_index, wt.slug, default=[])
        by_id = {m.session_id: m for m in index}
        newest = self._newest(index)
        wt.latest_session = newest

        # Collect EVERY agent pane sitting in this worktree (a worktree may host
        # more than one agent). claude panes get the full identity/summary/idle/
        # event logic; non-claude agents are kind + scrape only.
        matched = [p for p in agent_panes if _resolve(p.current_path) == wt_path]
        agents: list[LiveAgent] = []
        # The newest-session heuristic may only be *borrowed* by the first hookless
        # claude pane: two claudes in one dir would otherwise both adopt the same
        # session and render as identical rows. Later hookless panes get no meta.
        heuristic_taken = False
        for pane in matched:
            kind = self._agent_kind(pane)
            if kind is AgentKind.CLAUDE:
                agent = self._build_live(
                    pane,
                    wt,
                    by_id,
                    None if heuristic_taken else newest,
                    pane_map,
                    latest_events,
                )
                if pane.pane_id not in pane_map:
                    heuristic_taken = True
                agents.append(agent)
            elif kind is not None:
                agents.append(self._build_foreign_agent(pane, kind))

        if agents:
            wt.lifecycle = Lifecycle.LIVE
            wt.agents = agents
            wt.live = self._pick_primary(agents)
        elif self._match_pane(wt_path, panes) is not None:
            # A tmux pane sits in this worktree but no agent runs there.
            wt.lifecycle = Lifecycle.OPEN
        else:
            wt.lifecycle = Lifecycle.DORMANT

    # ---------------------------------------------------------------- identity

    def _build_live(
        self,
        pane: PaneInfo,
        wt: Worktree,
        by_id: dict[str, SessionMeta],
        newest: Optional[SessionMeta],
        pane_map: dict,
        latest_events: dict,
    ) -> LiveAgent:
        # Identity: pane map is authoritative; else newest session for the slug.
        session_id: Optional[str] = None
        entry = pane_map.get(pane.pane_id)
        authoritative = entry is not None
        if entry is not None:
            session_id = entry.session_id
        elif newest is not None:
            session_id = newest.session_id

        meta = by_id.get(session_id) if session_id else None
        if meta is None and not authoritative:
            # Only the heuristic identity may borrow the newest session's metadata.
            # An authoritative pane-map session_id that the index has not caught up
            # to yet must NOT be labeled with a different conversation's summary/idle
            # age — leave meta None until the index records it.
            meta = newest
        summary = meta.summary if meta is not None else None

        live = LiveAgent(
            pane_id=pane.pane_id,
            session_name=pane.session_name,
            window_index=pane.window_index,
            pid=pane.pid,
            cwd=pane.current_path,
            kind=AgentKind.CLAUDE,
            session_id=session_id,
            summary=summary,
        )

        # Idle age from the transcript's mtime.
        mtime = self._transcript_mtime(meta)
        live.idle_seconds = self._idle_seconds_from_mtime(mtime)

        # Activity: hook event is primary.
        event = latest_events.get(pane.pane_id)
        if event is not None:
            live.activity = _EVENT_ACTIVITY.get(event.kind, Activity.UNKNOWN)

        # Scrape fills extras and provides the activity fallback.
        result = self._apply_scrape(live)

        # Recompute activity from scrape + idle age when there is no authoritative
        # event, OR when the recorded event is stale. Only SessionStart/SessionEnd/
        # Notification/Stop hooks fire (hooks.EVENT_KINDS), so no hook is emitted
        # when the operator resolves a permission prompt or submits a fresh prompt.
        # If the transcript was written *after* the last event, that unrecorded
        # activity supersedes it — otherwise a resolved Notification stays "waiting"
        # and a post-Stop resume stays "idle" while claude is actually running.
        if live.activity == Activity.UNKNOWN or self._event_superseded(event, mtime):
            live.activity = self._scrape_activity(result, live.idle_seconds)

        return live

    # ------------------------------------------------------------------ scrape

    def _build_foreign_agent(self, pane: PaneInfo, kind: AgentKind) -> LiveAgent:
        """Build a non-claude agent: kind + screen-scrape extras only.

        Deliberately no ``session_id``/``summary``/``idle_seconds`` — those come
        from claude-specific infra (session index, hooks, transcript mtime) the
        other agents don't share. Activity defaults to ``UNKNOWN`` rather than
        RUNNING: claude's footer regexes almost never match a foreign pane, so a
        RUNNING default would show a permanent false "running", and the generic
        prompt regexes can false-positive to "waiting" on an arbitrary TUI. A LIVE
        worktree still shows a green marker; there is just no activity word.
        Non-claude waiting-detection is out of scope for v1.
        """
        live = LiveAgent(
            pane_id=pane.pane_id,
            session_name=pane.session_name,
            window_index=pane.window_index,
            pid=pane.pid,
            cwd=pane.current_path,
            kind=kind,
            activity=Activity.UNKNOWN,
        )
        self._apply_scrape(live)  # fills model/context/cost/elapsed if they parse
        return live

    @staticmethod
    def _pick_primary(agents: list[LiveAgent]) -> LiveAgent:
        """The representative agent for the row + status counts: claude if any."""
        for agent in agents:
            if agent.kind is AgentKind.CLAUDE:
                return agent
        return agents[0]

    def _apply_scrape(self, live: LiveAgent) -> Optional[ScrapeResult]:
        """Capture + parse the footer, filling non-None extras. Never raises."""
        try:
            captured = tmux.capture_pane(live.pane_id)
        except Exception:
            return None
        try:
            result = scrape.parse_footer(captured)
        except Exception:
            # parse_footer is contracted never to raise, but guard regardless.
            return None
        if result is None:
            return None
        if result.model is not None:
            live.model = result.model
        if result.context_pct is not None:
            live.context_pct = result.context_pct
        if result.cost_usd is not None:
            live.cost_usd = result.cost_usd
        if result.elapsed is not None:
            live.elapsed = result.elapsed
        return result

    # ------------------------------------------------------------------ helpers

    def _agent_kind(self, pane: PaneInfo) -> Optional[AgentKind]:
        """Classify a pane's foreground command into an AgentKind, or None.

        Uses the full command line (via ``classify_pane``) so a node-hosted agent
        (copilot/gemini/opencode) is not mistaken for claude's ``node`` disguise.
        Reuses the per-snapshot process table cached in ``_procs``.
        """
        try:
            return tmux.classify_pane(pane, getattr(self, "_procs", None))
        except Exception:
            return None

    @staticmethod
    def _match_pane(wt_path: Path, panes: list[PaneInfo]) -> Optional[PaneInfo]:
        for pane in panes:
            if _resolve(pane.current_path) == wt_path:
                return pane
        return None

    @staticmethod
    def _newest(index: list[SessionMeta]) -> Optional[SessionMeta]:
        if not index:
            return None
        try:
            return max(index, key=lambda m: m.modified)
        except Exception:
            return index[0]

    @staticmethod
    def _transcript_mtime(meta: Optional[SessionMeta]) -> Optional[float]:
        """Epoch mtime of the session transcript; None if unknown."""
        if meta is None:
            return None
        try:
            return Path(meta.jsonl_path).stat().st_mtime
        except Exception:
            # Fall back to the index-recorded modified epoch if the file is gone.
            try:
                return float(meta.modified)
            except Exception:
                return None

    @staticmethod
    def _idle_seconds_from_mtime(mtime: Optional[float]) -> Optional[int]:
        if mtime is None:
            return None
        return max(0, int(time.time() - mtime))

    @staticmethod
    def _event_superseded(event, mtime: Optional[float]) -> bool:
        """True if the transcript was written after the last hook event fired.

        The hook records the event ts at fire time; a later transcript mtime means
        claude produced output the hooks did not observe (prompt resolved / new
        prompt submitted), so the event-derived activity is stale.
        """
        if event is None or mtime is None:
            return False
        try:
            return mtime > float(event.ts)
        except Exception:
            return False

    @staticmethod
    def _scrape_activity(
        result: Optional[ScrapeResult], idle_seconds: Optional[int]
    ) -> Activity:
        """Derive activity from scrape + idle age: waiting > idle-threshold > running."""
        if result is not None and result.waiting:
            return Activity.WAITING
        if idle_seconds is not None and idle_seconds >= IDLE_THRESHOLD_SECONDS:
            return Activity.IDLE
        return Activity.RUNNING

    def _latest_events_by_pane(self) -> dict:
        """Latest StatusEvent per tmux pane (last write wins by ts)."""
        events = self._safe(panemap.read_events, default=[])
        latest: dict = {}
        for ev in events:
            prev = latest.get(ev.tmux_pane)
            if prev is None or ev.ts >= prev.ts:
                latest[ev.tmux_pane] = ev
        return latest

    @staticmethod
    def _safe(fn, *args, default):
        """Call ``fn`` returning ``default`` on any failure (degrade gracefully)."""
        try:
            return fn(*args)
        except Exception:
            return default
