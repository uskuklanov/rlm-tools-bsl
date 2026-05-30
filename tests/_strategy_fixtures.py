"""Shared test constants for strategy + rlm_help tests.

Filename starts with ``_`` so pytest's collector skips it; tests import the
constants by name. This avoids the older anti-pattern of importing test
fixtures from another ``test_*`` module (which made test ordering brittle).
"""

from __future__ import annotations


# Mock helper registry covering one entry per category so the strategy
# renderer exercises every section. Used by test_strategy_examples.py and
# tests/test_strategy_slim.py / tests/test_strategy_mode_env.py.
_MOCK_REGISTRY: dict = {
    "find_module": {
        "fn": None,
        "sig": "find_module(name) -> [{path, category, object_name, module_type}]",
        "cat": "discovery",
        "kw": [],
        "recipe": "",
    },
    "find_by_type": {
        "fn": None,
        "sig": "find_by_type(category, name='') -> same",
        "cat": "discovery",
        "kw": [],
        "recipe": "",
    },
    "extract_procedures": {
        "fn": None,
        "sig": "extract_procedures(path) -> [{name, type, line}]",
        "cat": "code",
        "kw": [],
        "recipe": "",
    },
    "find_exports": {"fn": None, "sig": "find_exports(path) -> [{name, line}]", "cat": "code", "kw": [], "recipe": ""},
    "find_callers_context": {
        "fn": None,
        "sig": "find_callers_context(proc, module_hint, 0, 50) -> {callers, _meta}",
        "cat": "code",
        "kw": [],
        "recipe": "",
    },
    "parse_object_xml": {
        "fn": None,
        "sig": "parse_object_xml(path) -> {name, synonym, attributes}",
        "cat": "xml",
        "kw": [],
        "recipe": "",
    },
    "analyze_object": {
        "fn": None,
        "sig": "analyze_object(name) -> full profile",
        "cat": "composite",
        "kw": [],
        "recipe": "",
    },
    "find_event_subscriptions": {
        "fn": None,
        "sig": "find_event_subscriptions(obj) -> [{event, handler}]",
        "cat": "business",
        "kw": [],
        "recipe": "",
    },
    "detect_extensions": {
        "fn": None,
        "sig": "detect_extensions() -> {config_role, warnings}",
        "cat": "extension",
        "kw": [],
        "recipe": "",
    },
    "help": {"fn": None, "sig": "help(task='') -> str", "cat": "navigation", "kw": [], "recipe": ""},
}


# Index-statistics dict shaped like a real builder_v12 output — exercises
# the "speedup summary" branch of the strategy INDEX block.
_REALISTIC_IDX_STATS: dict = {
    "methods": 12000,
    "calls": 80000,
    "config_name": "ТестоваяКонфигурация",
    "config_version": "1.2.3",
    "has_fts": True,
    "builder_version": 12,
    "object_synonyms": 1234,
    "object_attributes": 4500,
    "predefined_items": 220,
    "form_elements": 4000,
    "register_movements": 1100,
    "role_rights": 700,
    "file_paths": 21000,
}
