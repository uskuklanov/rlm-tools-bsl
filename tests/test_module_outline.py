"""Tests for get_module_outline (v1.20.0) — the cheap module #Область skeleton.

Covers the pure ``_build_outline_tree`` (interval-nesting, ``end_line=None`` via
``+inf``, orphans, crossing intervals, ``include_methods``), the
``IndexReader.get_outline_data`` reader (sentinel ``None`` when ``regions`` is
unreadable), and the ``get_module_outline`` helper routing (index path, the three
fallback reasons, live parsing).
"""

import os
import textwrap

import pytest

from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.bsl_helpers import _build_outline_tree, make_bsl_helpers
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


_CONFIG_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
        <Configuration><Properties><Name>Конф</Name><NamePrefix/></Properties></Configuration>
    </MetaDataObject>
""")

# Nested regions + one orphan method (code outside any #Область).
_CM_OUTLINE_BSL = textwrap.dedent("""\
    #Область ПрограммныйИнтерфейс

    Функция ПубличнаяФункция(А) Экспорт
        Возврат А;
    КонецФункции

    #Область Вложенная

    Процедура ВложеннаяПроцедура()
        // тело
    КонецПроцедуры

    #КонецОбласти

    #КонецОбласти

    #Область СлужебныеПроцедурыИФункции

    Процедура СлужебнаяПроцедура()
        // тело
    КонецПроцедуры

    #КонецОбласти

    Процедура МетодВнеОбластей()
        // orphan
    КонецПроцедуры
""")

# Unclosed #Область → end_line=None in the index.
_CM_UNCLOSED_BSL = textwrap.dedent("""\
    #Область Открытая

    Процедура МетодВОткрытой() Экспорт
        // тело
    КонецПроцедуры
""")

# No regions at all → all methods are orphans.
_CM_FLAT_BSL = textwrap.dedent("""\
    Процедура ПлоскийМетод() Экспорт
        // тело
    КонецПроцедуры
""")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_project(tmp_path):
    cf = os.path.join(tmp_path, "src", "cf")
    _write(os.path.join(cf, "Configuration.xml"), _CONFIG_XML)
    _write(os.path.join(cf, "CommonModules", "Главный", "Ext", "Module.bsl"), _CM_OUTLINE_BSL)
    _write(os.path.join(cf, "CommonModules", "Открытый", "Ext", "Module.bsl"), _CM_UNCLOSED_BSL)
    _write(os.path.join(cf, "CommonModules", "Плоский", "Ext", "Module.bsl"), _CM_FLAT_BSL)
    return cf


def _make_bsl(base, **kwargs):
    helpers, resolve_safe = make_helpers(str(base))
    format_info = detect_format(str(base))
    return make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        **kwargs,
    )


def _path_of(bsl, object_name):
    mods = bsl["find_module"](object_name)
    return next(m["path"] for m in mods if m["path"].endswith("Module.bsl"))


@pytest.fixture
def built(tmp_path):
    cf = _make_project(tmp_path)
    db_path = IndexBuilder().build(cf, build_calls=True, build_metadata=True, build_fts=True)
    reader = IndexReader(db_path)
    bsl = _make_bsl(cf, idx_reader=reader)
    yield bsl, reader, cf
    reader.close()


@pytest.fixture
def live(tmp_path):
    cf = _make_project(tmp_path)
    return _make_bsl(cf)


# ---------------------------------------------------------------------------
# Pure tree function (no DB)
# ---------------------------------------------------------------------------


def test_tree_nested_and_totals():
    regions = [
        {"name": "Внешняя", "line": 1, "end_line": 30},
        {"name": "Вложенная", "line": 5, "end_line": 15},
    ]
    methods = [
        {"name": "f1", "type": "Функция", "is_export": True, "line": 2, "end_line": 4, "loc": 3},
        {"name": "f2", "type": "Процедура", "is_export": False, "line": 7, "end_line": 12, "loc": 6},
    ]
    outline, orphan = _build_outline_tree(regions, methods, include_methods=True)
    assert [r["region"] for r in outline] == ["Внешняя"]
    outer = outline[0]
    assert [c["region"] for c in outer["children"]] == ["Вложенная"]
    assert [m["name"] for m in outer["methods"]] == ["f1"]
    assert [m["name"] for m in outer["children"][0]["methods"]] == ["f2"]
    # bottom-up: outer counts its own f1 + nested f2
    assert outer["totals"] == {"methods": 2, "exports": 1}
    assert outer["children"][0]["totals"] == {"methods": 1, "exports": 0}
    assert orphan == []


def test_tree_orphan_method():
    regions = [{"name": "Об", "line": 1, "end_line": 5}]
    methods = [{"name": "вне", "type": "Процедура", "is_export": False, "line": 10, "end_line": 12, "loc": 3}]
    outline, orphan = _build_outline_tree(regions, methods, include_methods=True)
    assert outline[0]["totals"]["methods"] == 0
    assert [m["name"] for m in orphan] == ["вне"]


def test_tree_end_line_none_no_typeerror():
    """Codex #4: end_line=None (unclosed region) → +inf for containment, no crash."""
    regions = [{"name": "Открытая", "line": 1, "end_line": None}]
    methods = [{"name": "m", "type": "Процедура", "is_export": True, "line": 3, "end_line": 5, "loc": 3}]
    outline, orphan = _build_outline_tree(regions, methods, include_methods=True)
    assert outline[0]["end_line"] is None  # reported as-is, honest about unclosed
    assert [m["name"] for m in outline[0]["methods"]] == ["m"]  # contained via +inf
    assert orphan == []


def test_tree_crossing_intervals_become_roots():
    regions = [
        {"name": "A", "line": 1, "end_line": 10},
        {"name": "B", "line": 5, "end_line": 15},  # crosses A (not nested)
    ]
    outline, _orphan = _build_outline_tree(regions, [], include_methods=True)
    assert {r["region"] for r in outline} == {"A", "B"}  # both roots
    assert all(not r["children"] for r in outline)


def test_tree_include_methods_false():
    regions = [{"name": "Об", "line": 1, "end_line": 10}]
    methods = [{"name": "m", "type": "Процедура", "is_export": False, "line": 2, "end_line": 4, "loc": 3}]
    outline, orphan = _build_outline_tree(regions, methods, include_methods=False)
    assert "methods" not in outline[0]
    assert "totals" in outline[0]
    assert orphan == []


# ---------------------------------------------------------------------------
# Index path
# ---------------------------------------------------------------------------


def test_index_nested_tree_and_aggregates(built):
    bsl, _r, _cf = built
    o = bsl["get_module_outline"](_path_of(bsl, "Главный"))
    assert o["_meta"] == {"index_used": True, "fallback_reason": None}
    assert o["category"] == "CommonModules" and o["object_name"] == "Главный"
    assert o["totals"]["methods"] == 4
    assert o["totals"]["exports"] == 1
    assert o["totals"]["regions"] == 3
    assert {r["region"] for r in o["outline"]} == {"ПрограммныйИнтерфейс", "СлужебныеПроцедурыИФункции"}
    pi = next(r for r in o["outline"] if r["region"] == "ПрограммныйИнтерфейс")
    assert pi["totals"] == {"methods": 2, "exports": 1}  # incl. nested region
    assert [c["region"] for c in pi["children"]] == ["Вложенная"]
    assert [m["name"] for m in pi["methods"]] == ["ПубличнаяФункция"]
    assert [m["name"] for m in pi["children"][0]["methods"]] == ["ВложеннаяПроцедура"]


def test_index_orphan_methods(built):
    bsl, _r, _cf = built
    o = bsl["get_module_outline"](_path_of(bsl, "Главный"))
    assert [m["name"] for m in o["orphan_methods"]] == ["МетодВнеОбластей"]


def test_include_methods_false_drops_leaves(built):
    bsl, _r, _cf = built
    o = bsl["get_module_outline"](_path_of(bsl, "Главный"), include_methods=False)
    assert "orphan_methods" not in o
    for r in o["outline"]:
        assert "methods" not in r and "totals" in r
    assert o["totals"]["methods"] == 4  # module totals still computed


def test_index_unclosed_region_end_line_none(built):
    """Codex #4 on a real index: end_line=NULL region → no crash, reported None."""
    bsl, reader, _cf = built
    path = _path_of(bsl, "Открытый")
    data = reader.get_outline_data(path)
    assert data["regions"][0]["end_line"] is None  # stored as NULL
    o = bsl["get_module_outline"](path)
    assert o["_meta"]["index_used"] is True
    region = o["outline"][0]
    assert region["region"] == "Открытая" and region["end_line"] is None
    assert [m["name"] for m in region["methods"]] == ["МетодВОткрытой"]


def test_index_flat_module_methods_are_orphans(built):
    """0 regions (regions query returned empty) → valid outline, NOT a fallback."""
    bsl, _r, _cf = built
    o = bsl["get_module_outline"](_path_of(bsl, "Плоский"))
    assert o["_meta"]["index_used"] is True
    assert o["totals"]["regions"] == 0
    assert o["outline"] == []
    assert [m["name"] for m in o["orphan_methods"]] == ["ПлоскийМетод"]


def test_determinism(built):
    bsl, _r, _cf = built
    p = _path_of(bsl, "Главный")
    assert bsl["get_module_outline"](p) == bsl["get_module_outline"](p)


# ---------------------------------------------------------------------------
# Routing to live fallback (codex round-3: never a silently-empty outline)
# ---------------------------------------------------------------------------


def test_missing_regions_table_routes_live(built, monkeypatch):
    """R3-1: reader returns sentinel None (regions table absent) → live, with a
    distinct fallback_reason — NOT a silently-empty {regions: []}."""
    bsl, reader, _cf = built
    monkeypatch.setattr(reader, "get_outline_data", lambda p: None)
    o = bsl["get_module_outline"](_path_of(bsl, "Главный"))
    assert o["_meta"]["index_used"] is False
    assert o["_meta"]["fallback_reason"] == "index_unavailable_or_table_missing"
    # live still rebuilt the real tree
    assert o["totals"]["methods"] == 4
    assert {r["region"] for r in o["outline"]} == {"ПрограммныйИнтерфейс", "СлужебныеПроцедурыИФункции"}


def test_stale_module_empty_methods_routes_live(built, monkeypatch):
    """Module row present but methods empty (stale) → live safety net."""
    bsl, reader, _cf = built
    path = _path_of(bsl, "Главный")
    monkeypatch.setattr(
        reader,
        "get_outline_data",
        lambda p: {
            "module": {"category": "CommonModules", "object_name": "Главный", "module_type": "Module"},
            "regions": [],
            "methods": [],
        },
    )
    o = bsl["get_module_outline"](path)
    assert o["_meta"]["index_used"] is False
    assert o["_meta"]["fallback_reason"] == "index_empty_for_module"
    assert o["totals"]["methods"] == 4  # live found the real methods


def test_module_not_in_index_routes_live(built, monkeypatch):
    bsl, reader, _cf = built
    monkeypatch.setattr(reader, "get_outline_data", lambda p: {"module": None, "regions": [], "methods": []})
    o = bsl["get_module_outline"](_path_of(bsl, "Главный"))
    assert o["_meta"]["index_used"] is False
    assert o["_meta"]["fallback_reason"] == "index_unavailable_or_table_missing"


# ---------------------------------------------------------------------------
# Live fallback (no index)
# ---------------------------------------------------------------------------


def test_live_fallback_same_shape(live):
    o = live["get_module_outline"](_path_of(live, "Главный"))
    assert o["_meta"]["index_used"] is False
    assert o["totals"]["methods"] == 4
    assert o["totals"]["regions"] == 3
    assert {r["region"] for r in o["outline"]} == {"ПрограммныйИнтерфейс", "СлужебныеПроцедурыИФункции"}
    assert [m["name"] for m in o["orphan_methods"]] == ["МетодВнеОбластей"]


def test_live_unclosed_region(live):
    o = live["get_module_outline"](_path_of(live, "Открытый"))
    assert o["_meta"]["index_used"] is False
    assert o["outline"][0]["region"] == "Открытая"
    assert o["outline"][0]["end_line"] is None


def test_live_fallback_fills_metadata_without_prior_ensure_index(tmp_path):
    """Finding: a DIRECT get_module_outline (no prior find_module → _index_state
    still empty, and the live path never calls _ensure_index) must still fill
    category/object_name/module_type via parse_bsl_path — not leave them None."""
    cf = _make_project(tmp_path)
    bsl = _make_bsl(cf)  # fresh closure, no idx_reader, _index_state empty
    o = bsl["get_module_outline"]("CommonModules/Главный/Ext/Module.bsl")  # NO find_module first
    assert o["_meta"]["index_used"] is False
    assert o["category"] == "CommonModules"
    assert o["object_name"] == "Главный"
    assert o["module_type"] == "Module"
    assert o["totals"]["methods"] == 4  # structural data still correct
