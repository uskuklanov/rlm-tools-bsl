"""Sanity checks for src/rlm_tools_bsl/bsl_strategy_data.py.

These guard against accidental drift when editing the strategy text — the
slim-mode `rlm_help` MCP tool reads sections from this module, so silent
content changes here would break agent guidance.
"""

from __future__ import annotations

from rlm_tools_bsl.bsl_strategy_data import DISAMBIGUATION_PAIRS, STRATEGY_SECTIONS


def test_disambiguation_pairs_count():
    assert len(DISAMBIGUATION_PAIRS) == 11


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

    # v1.12.0: Step 5 extensions phrasing — must mention high-level helpers
    # in BOTH slim (STRATEGY_SECTIONS["workflow"]) and full (_STRATEGY_HEADER).
    ext_marker_slim = "to high-level BSL helpers"
    assert ext_marker_slim in STRATEGY_SECTIONS["workflow"]
    assert ext_marker_slim in _STRATEGY_HEADER
    # And the explicit PermissionError warning remains.
    assert "PermissionError" in STRATEGY_SECTIONS["workflow"]
    assert "PermissionError" in _STRATEGY_HEADER


# v1.12.0 extension visibility — explicit checks per round-6 review.


def test_strategy_sections_slim_mentions_extension_helpers():
    section = STRATEGY_SECTIONS["workflow"]
    for needle in ("read_procedure", "extract_procedures", "parse_object_xml", "find_predefined"):
        assert needle in section


def test_strategy_text_full_mentions_extension_helpers():
    from rlm_tools_bsl.bsl_knowledge import _STRATEGY_HEADER

    for needle in ("read_procedure", "extract_procedures", "parse_object_xml", "find_predefined"):
        assert needle in _STRATEGY_HEADER


def test_rlm_help_topic_extensions_does_not_suggest_read_file_on_ext_paths():
    from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES

    recipe = _BUSINESS_RECIPES["расширения"]
    full_text = " ".join(recipe["full"])
    compact_text = " ".join(recipe["compact"])
    # No "read_file('../...')" suggestion.
    assert "read_file('../" not in full_text
    assert "read_file('../" not in compact_text
    # Helpers mentioned explicitly somewhere.
    assert "read_procedure" in full_text + compact_text
    assert "extract_procedures" in full_text + compact_text


def test_extension_critical_block_mentions_new_phrasing():
    from types import SimpleNamespace

    from rlm_tools_bsl.bsl_knowledge import _extension_strategy
    from rlm_tools_bsl.extension_detector import ConfigRole

    ctx = SimpleNamespace(
        current=SimpleNamespace(role=ConfigRole.MAIN, name="MainCfg", purpose="", name_prefix=""),
        nearby_extensions=[
            SimpleNamespace(name="ExtAddOn", name_prefix="ext_", path="/tmp/cfe/ExtAddOn"),
        ],
        nearby_main=None,
    )
    text = _extension_strategy(ctx, {})
    assert "PermissionError" in text
    assert "read_procedure" in text and "extract_procedures" in text
