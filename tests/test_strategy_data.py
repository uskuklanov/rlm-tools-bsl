"""Sanity checks for src/rlm_tools_bsl/bsl_strategy_data.py.

These guard against accidental drift when editing the strategy text — the
slim-mode `rlm_help` MCP tool reads sections from this module, so silent
content changes here would break agent guidance.
"""

from __future__ import annotations

from rlm_tools_bsl.bsl_strategy_data import DISAMBIGUATION_PAIRS, STRATEGY_SECTIONS


def test_disambiguation_pairs_count():
    assert len(DISAMBIGUATION_PAIRS) == 8


def test_disambiguation_pair_shape():
    required_keys = {"pair", "summary", "when_a", "when_b", "rule", "tags"}
    for entry in DISAMBIGUATION_PAIRS:
        assert required_keys.issubset(entry.keys()), entry
        a, b = entry["pair"]
        assert isinstance(a, str) and isinstance(b, str)
        assert isinstance(entry["summary"], str) and entry["summary"]
        assert isinstance(entry["rule"], str) and entry["rule"]
        assert isinstance(entry["tags"], list)


def test_strategy_sections_keys():
    # The dispatcher routes section='disambiguation' separately to the
    # structured DISAMBIGUATION_PAIRS list — that key must NOT live in
    # STRATEGY_SECTIONS.
    assert set(STRATEGY_SECTIONS.keys()) == {"workflow", "performance", "batching", "io", "critical"}


def test_strategy_sections_values_nonempty():
    for k, v in STRATEGY_SECTIONS.items():
        assert isinstance(v, str) and v.strip(), f"empty STRATEGY_SECTIONS[{k!r}]"


def test_strategy_sections_did_not_drift_from_legacy():
    # Sanity: each section's marker / leading line still appears in the
    # legacy strategy header. Not byte-for-byte — just guards against
    # accidental rename or deletion. _STRATEGY_HEADER + _STRATEGY_IO_SECTION
    # are the source of truth in legacy mode.
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER, _STRATEGY_IO_SECTION

    assert "== CRITICAL ==" in STRATEGY_SECTIONS["critical"]
    assert "== CRITICAL ==" in _STRATEGY_HEADER

    assert "== WORKFLOW ==" in STRATEGY_SECTIONS["workflow"]
    assert "Step 0 — UNDERSTAND" in STRATEGY_SECTIONS["workflow"]
    assert "Step 0 — UNDERSTAND" in _STRATEGY_HEADER

    assert "== STEP 4 EXTENDED" in STRATEGY_SECTIONS["performance"]
    assert "== STEP 4 EXTENDED" in _STRATEGY_HEADER

    assert "== BATCHING & OUTPUT ==" in STRATEGY_SECTIONS["batching"]
    assert "== BATCHING & OUTPUT ==" in _STRATEGY_HEADER

    assert "File I/O:" in STRATEGY_SECTIONS["io"]
    assert "File I/O:" in _STRATEGY_IO_SECTION
