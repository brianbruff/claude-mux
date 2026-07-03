"""Pure, version-tolerant parsing of a claude pane footer. MUST NEVER raise.

The parser reads the rendered screen of a claude pane (as produced by
``tmux capture-pane``) and best-effort extracts the fields shown in the footer.
It is deliberately defensive: Claude Code's TUI layout is not a stable contract
(multiple versions run side by side), so every field is optional and *any*
failure degrades to ``None`` rather than raising. See ADR-0001.

Reference footer this targets::

    [Opus 4.8] 📁 mu-power-analyst | 🌿 main
    ░░░░░░░░░░ 5% | $1.04 | ⏱️ 2m 59s
    ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ScrapeResult:
    """Fields scraped from a claude pane footer; any unknown field is None."""

    model: str | None
    project: str | None
    branch: str | None
    context_pct: int | None
    cost_usd: float | None
    elapsed: str | None
    waiting: bool | None


# --- Field patterns (compiled once; all matching is best-effort) ------------

# Any bracketed run, e.g. "[Opus 4.8]". We later pick the first that looks
# like a model name (contains a letter).
_BRACKET_RE = re.compile(r"\[([^\[\]]{1,60})\]")

# Prefer a bracket that actually names a known model family.
_MODEL_HINT_RE = re.compile(r"opus|sonnet|haiku", re.IGNORECASE)

# 📁 <project> — capture up to a separator / EOL. U+1F4C1.
_PROJECT_RE = re.compile(r"\U0001F4C1\s*([^|\n\r]+)")

# 🌿 <branch> — capture up to a separator / EOL. U+1F33F.
_BRANCH_RE = re.compile(r"\U0001F33F\s*([^|\n\r]+)")

# "5%" context percentage.
_CONTEXT_RE = re.compile(r"(\d{1,3})\s*%")

# "$1.04" cost in USD.
_COST_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")

# A duration like "2m 59s", "1h 2m 3s", "59s", "45m".
_TIME_TOKENS = r"\d+\s*[hms](?:\s*\d+\s*[hms])*"

# ⏱ (U+23F1) optionally followed by the variation selector U+FE0F, then a time.
_ELAPSED_RE = re.compile(r"⏱️?\s*(" + _TIME_TOKENS + r")")

# Fallback: a bare duration anywhere (used only if the stopwatch is absent).
_TIME_FALLBACK_RE = re.compile(r"\b(" + _TIME_TOKENS + r")\b")

# Permission / confirmation prompt markers -> waiting.
_WAITING_RES = [
    re.compile(r"\bdo you want\b", re.IGNORECASE),
    re.compile(r"\bwant to proceed\b", re.IGNORECASE),
    re.compile(r"\ballow this\b", re.IGNORECASE),
    re.compile(r"\d+\.\s*yes\b", re.IGNORECASE),          # "1. Yes"
    re.compile(r"[❯›>]\s*\d+\.\s"),                        # selected menu item
    re.compile(r"\(\s*y\s*/\s*n\s*\)", re.IGNORECASE),   # (y/n)
    re.compile(r"\[\s*y\s*/\s*n\s*\]", re.IGNORECASE),   # [y/N]
]

# Signals that the pane is in a normal (not-waiting) state -> waiting=False.
_NOT_WAITING_RES = [
    re.compile(r"auto mode", re.IGNORECASE),
    re.compile(r"esc to interrupt", re.IGNORECASE),
    re.compile(r"shift\+tab to cycle", re.IGNORECASE),
    re.compile(r"\? for shortcuts", re.IGNORECASE),
]

_WS_RE = re.compile(r"\s+")


def _extract_model(text: str) -> str | None:
    matches = [m.group(1).strip() for m in _BRACKET_RE.finditer(text)]
    if not matches:
        return None
    # Prefer an explicit model family name.
    for candidate in matches:
        if _MODEL_HINT_RE.search(candidate):
            return candidate
    # Otherwise the first bracket that contains a letter (skip pure symbols/nums).
    for candidate in matches:
        if any(ch.isalpha() for ch in candidate):
            return candidate
    return None


def _extract_first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def _extract_context_pct(text: str) -> int | None:
    m = _CONTEXT_RE.search(text)
    if not m:
        return None
    value = int(m.group(1))
    if 0 <= value <= 100:
        return value
    return None


def _extract_cost(text: str) -> float | None:
    m = _COST_RE.search(text)
    if not m:
        return None
    return float(m.group(1))


def _extract_elapsed(text: str) -> str | None:
    m = _ELAPSED_RE.search(text)
    if not m:
        m = _TIME_FALLBACK_RE.search(text)
    if not m:
        return None
    return _WS_RE.sub(" ", m.group(1).strip())


def _extract_waiting(text: str, found_footer: bool) -> bool | None:
    for pat in _WAITING_RES:
        if pat.search(text):
            return True
    for pat in _NOT_WAITING_RES:
        if pat.search(text):
            return False
    # If we recognised footer fields but saw no prompt, treat as not waiting.
    if found_footer:
        return False
    return None


def parse_footer(captured: str) -> ScrapeResult:
    """Parse a claude pane's last lines into a ScrapeResult; never throws."""
    try:
        if not captured or not isinstance(captured, str):
            return _fresh_empty()

        text = captured
        model = _extract_model(text)
        project = _extract_first(_PROJECT_RE, text)
        branch = _extract_first(_BRANCH_RE, text)
        context_pct = _extract_context_pct(text)
        cost_usd = _extract_cost(text)
        elapsed = _extract_elapsed(text)

        found_footer = any(
            v is not None
            for v in (model, project, branch, context_pct, cost_usd, elapsed)
        )
        waiting = _extract_waiting(text, found_footer)

        return ScrapeResult(
            model=model,
            project=project,
            branch=branch,
            context_pct=context_pct,
            cost_usd=cost_usd,
            elapsed=elapsed,
            waiting=waiting,
        )
    except Exception:
        return _fresh_empty()


def _fresh_empty() -> ScrapeResult:
    return ScrapeResult(
        model=None,
        project=None,
        branch=None,
        context_pct=None,
        cost_usd=None,
        elapsed=None,
        waiting=None,
    )
