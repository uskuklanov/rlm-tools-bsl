"""Router behaviour for ``RLM_STRATEGY_MODE`` env var: full / slim / invalid /
production-default. Plus a router-vs-legacy-builder equivalence guard.
"""

from __future__ import annotations

import pytest

from rlm_tools_bsl.bsl_knowledge import (
    _build_full_strategy,
    get_strategy,
    get_strategy_mode,
)
from _strategy_fixtures import _MOCK_REGISTRY, _REALISTIC_IDX_STATS


# ── Mode resolution ─────────────────────────────────────────────────────────


def test_mode_explicit_full(monkeypatch):
    monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
    assert get_strategy_mode() == "full"
    s = get_strategy("medium", None)
    # Markers that only the legacy/full strategy carries.
    assert "Step 0 — UNDERSTAND" in s
    assert "== DISAMBIGUATION ==" in s
    # No HELP block in full mode.
    assert "== HELP ==" not in s


def test_mode_explicit_slim(monkeypatch):
    monkeypatch.setenv("RLM_STRATEGY_MODE", "slim")
    assert get_strategy_mode() == "slim"
    s = get_strategy("medium", None)
    assert "== HELP ==" in s
    assert "rlm_help" in s


def test_mode_invalid_falls_back_to_slim(monkeypatch):
    monkeypatch.setenv("RLM_STRATEGY_MODE", "bogus")
    assert get_strategy_mode() == "slim"
    s = get_strategy("medium", None)
    assert "== HELP ==" in s


def test_mode_unset_falls_back_to_slim(monkeypatch):
    """Production default — when env var is missing slim wins.

    The autouse fixture ``_strategy_mode_default`` pins the env variable for
    every test; we delete it explicitly here to verify the resolver's own
    default. ``raising=False`` is required because monkeypatch removed the
    pre-existing pin already.
    """
    monkeypatch.delenv("RLM_STRATEGY_MODE", raising=False)
    assert get_strategy_mode() == "slim"
    s = get_strategy("medium", None)
    assert "== HELP ==" in s


# ── Router ≡ legacy in full mode ────────────────────────────────────────────


def test_router_full_matches_legacy_builder(monkeypatch):
    """Byte-for-byte: ``get_strategy(...)`` under full mode must equal a
    direct call to ``_build_full_strategy(...)`` with the same args. Catches
    any accidental router-level rewriting."""
    monkeypatch.setenv("RLM_STRATEGY_MODE", "full")

    args = (
        "high",
        None,
        ["рлф"],
        None,
        None,
        _MOCK_REGISTRY,
        _REALISTIC_IDX_STATS,
        ["index is N days old"],
        "себестоимость",
    )
    assert get_strategy(*args) == _build_full_strategy(*args)


# Default-mode-pin sanity: when the autouse fixture says "slim" the call
# routes to slim builder; when "full" it routes to legacy. Test verifies the
# fixture is wired correctly.


@pytest.mark.strategy_mode_slim
def test_marker_pins_slim():
    s = get_strategy("medium", None)
    assert "== HELP ==" in s
    assert "Step 0 — UNDERSTAND" not in s


def test_default_marker_is_full():
    # No marker → fixture sets RLM_STRATEGY_MODE=full.
    s = get_strategy("medium", None)
    assert "Step 0 — UNDERSTAND" in s
