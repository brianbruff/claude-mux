"""Unit tests for claude_mux.scrape.parse_footer (pure, must never raise)."""
from __future__ import annotations

from claude_mux.scrape import ScrapeResult, parse_footer

REFERENCE_FOOTER = (
    "[Opus 4.8] \U0001F4C1 mu-power-analyst | \U0001F33F main\n"
    "░░░░░░░░░░ 5% | $1.04 | ⏱️ 2m 59s\n"
    "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n"
)


def test_reference_footer_all_fields():
    r = parse_footer(REFERENCE_FOOTER)
    assert r.model == "Opus 4.8"
    assert r.project == "mu-power-analyst"
    assert r.branch == "main"
    assert r.context_pct == 5
    assert r.cost_usd == 1.04
    assert r.elapsed == "2m 59s"
    assert r.waiting is False


def test_never_raises_on_garbage():
    for junk in ["", "   ", "no footer here", "\x00\x01\x02", "[unclosed", "]]][[["]:
        assert isinstance(parse_footer(junk), ScrapeResult)


def test_empty_returns_all_none():
    r = parse_footer("")
    assert r == ScrapeResult(None, None, None, None, None, None, None)


def test_waiting_do_you_want():
    captured = (
        "Do you want to allow this action?\n"
        "❯ 1. Yes\n"
        "  2. No\n"
    )
    r = parse_footer(captured)
    assert r.waiting is True


def test_waiting_numbered_yes_menu():
    r = parse_footer("1. Yes\n2. Yes, and don't ask again\n3. No")
    assert r.waiting is True


def test_waiting_yn_prompt():
    assert parse_footer("Proceed? (y/n)").waiting is True


def test_waiting_none_when_no_footer_and_no_prompt():
    assert parse_footer("just some random text output").waiting is None


def test_model_prefers_family_name():
    # Even if another bracket precedes it, the model family wins.
    captured = "[12:30] some log [Sonnet 4.5] rest"
    assert parse_footer(captured).model == "Sonnet 4.5"


def test_partial_footer_fields_none():
    r = parse_footer("$0.00 | 12%")
    assert r.cost_usd == 0.0
    assert r.context_pct == 12
    assert r.model is None
    assert r.project is None
    assert r.branch is None


def test_context_pct_out_of_range_rejected():
    # A stray "999%" is not a valid context percentage.
    assert parse_footer("999%").context_pct is None


def test_elapsed_variants():
    assert parse_footer("⏱️ 1h 2m 3s").elapsed == "1h 2m 3s"
    assert parse_footer("⏱ 45s").elapsed == "45s"


def test_non_string_input_is_safe():
    assert parse_footer(None).waiting is None  # type: ignore[arg-type]
