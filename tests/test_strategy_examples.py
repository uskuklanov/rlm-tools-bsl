"""Tests that the strategy text contains workflow, helpers table, and key sections."""

from rlm_tools_bsl.bsl_knowledge import get_strategy
from _strategy_fixtures import _MOCK_REGISTRY  # noqa: F401  — shared across strategy tests


def _get_strategy_text():
    return get_strategy("medium", None, registry=_MOCK_REGISTRY)


def test_strategy_contains_workflow():
    s = _get_strategy_text()
    assert "WORKFLOW" in s


def test_strategy_contains_helpers_table():
    s = _get_strategy_text()
    assert "HELPERS" in s
    assert "Module discovery:" in s
    assert "Business logic:" in s
    assert "Extensions:" in s


def test_strategy_contains_print_calls():
    s = _get_strategy_text()
    assert "print()" in s


def test_strategy_contains_find_module_example():
    s = _get_strategy_text()
    assert "find_module(" in s


def test_strategy_contains_find_exports_example():
    s = _get_strategy_text()
    assert "find_exports(" in s


def test_strategy_contains_find_callers_context_example():
    s = _get_strategy_text()
    assert "find_callers_context(" in s


def test_strategy_contains_help_mention():
    s = _get_strategy_text()
    assert "help(" in s


def test_strategy_mentions_python_sandbox():
    s = _get_strategy_text()
    assert "Python" in s


def test_strategy_all_effort_levels():
    """All effort levels should produce strategy with workflow and helpers."""
    for effort in ("low", "medium", "high", "max"):
        s = get_strategy(effort, None, registry=_MOCK_REGISTRY)
        assert "WORKFLOW" in s, f"Missing WORKFLOW for effort={effort}"
        assert "HELPERS" in s, f"Missing HELPERS for effort={effort}"
        assert "CRITICAL" in s, f"Missing CRITICAL for effort={effort}"
