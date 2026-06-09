"""Tests for find_path — reachability over the call graph (v1.19.0, step 4a).

Also covers the additive per-edge ``edge_exact`` flag added to
``IndexReader.get_callers`` (the basis of find_path's ``precision``).
"""

import pytest

from rlm_tools_bsl import bsl_helpers as _bh
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


# Call chain: Корень → ОбщийА.Точка → Низ.НизкийМетод. ОбщийБ.Точка is a namesake
# that does NOT reach НизкийМетод (for hint disambiguation). Прямой.Дёрнуть makes
# an UNRESOLVABLE bare cross-module call (callee_key NULL → recall / edge_exact=False).
NIZ_BSL = """\
Процедура НизкийМетод() Экспорт
    Сообщить("низ");
КонецПроцедуры
"""

OBSHIY_A_BSL = """\
Процедура Точка() Экспорт
    Низ.НизкийМетод();
КонецПроцедуры
"""

OBSHIY_B_BSL = """\
Процедура Точка() Экспорт
    Сообщить("другой");
КонецПроцедуры
"""

VERH_BSL = """\
Процедура Корень() Экспорт
    ОбщийА.Точка();
КонецПроцедуры
"""

PRYAMOY_BSL = """\
Процедура Дёрнуть() Экспорт
    НизкийМетод();
КонецПроцедуры
"""

PROJECT_FILES = {
    "CommonModules/Низ/Ext/Module.bsl": NIZ_BSL,
    "CommonModules/ОбщийА/Ext/Module.bsl": OBSHIY_A_BSL,
    "CommonModules/ОбщийБ/Ext/Module.bsl": OBSHIY_B_BSL,
    "CommonModules/Верх/Ext/Module.bsl": VERH_BSL,
    "CommonModules/Прямой/Ext/Module.bsl": PRYAMOY_BSL,
}


def _write_project(root):
    for rel, content in PROJECT_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
    (root / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")


def _indexed(base, db_path):
    reader = IndexReader(db_path)
    helpers, resolve_safe = make_helpers(str(base))
    fmt = detect_format(str(base))
    bsl = make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
        idx_reader=reader,
        idx_zero_callers_authoritative=True,
    )
    return bsl, reader


def _fs(base):
    helpers, resolve_safe = make_helpers(str(base))
    fmt = detect_format(str(base))
    return make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
    )


@pytest.fixture
def project(tmp_path):
    _write_project(tmp_path)
    return tmp_path


@pytest.fixture
def built(project):
    db_path = IndexBuilder().build(str(project), build_calls=True)
    bsl, reader = _indexed(project, db_path)
    yield bsl, reader, str(project), db_path
    reader.close()


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


def test_path_found_forward_chain(built):
    bsl, *_ = built
    res = bsl["find_path"]("Корень", "НизкийМетод")
    assert res["found"] is True
    names = [el["name"] for el in res["path"]]
    assert names == ["Корень", "Точка", "НизкийМетод"]
    assert res["depth"] == 2


def test_call_line_is_edge_metadata(built):
    bsl, *_ = built
    res = bsl["find_path"]("Корень", "НизкийМетод")
    path = res["path"]
    # Intermediate nodes carry the call line to the NEXT node; terminal = None.
    assert path[0]["call_line"] is not None  # Корень → Точка
    assert path[1]["call_line"] is not None  # Точка → НизкийМетод
    assert path[-1]["call_line"] is None  # to (НизкийМетод) is terminal


def test_unrelated_pair_not_found(built):
    bsl, *_ = built
    res = bsl["find_path"]("НизкийМетод", "Корень")
    assert res["found"] is False
    assert res["path"] is None


def test_precision_exact_on_resolved_chain(built):
    bsl, *_ = built
    res = bsl["find_path"]("Корень", "НизкийМетод")
    assert res["found"] is True
    assert res["_meta"]["precision"] == "exact"
    assert res["_meta"]["to_exact"] is True


def test_precision_heuristic_on_fs_fallback(project):
    bsl = _fs(project)  # no idx_reader → FS scan, name-based
    res = bsl["find_path"]("Корень", "НизкийМетод")
    assert res["found"] is True
    assert res["_meta"]["precision"] == "heuristic"
    assert res["_meta"]["to_exact"] is False


# ---------------------------------------------------------------------------
# Hint disambiguation
# ---------------------------------------------------------------------------


def test_from_hint_disambiguates_namesake(built):
    bsl, *_ = built
    # ОбщийА.Точка reaches НизкийМетод; ОбщийБ.Точка does not.
    hit = bsl["find_path"]("Точка", "НизкийМетод", from_hint="ОбщийА")
    assert hit["found"] is True
    miss = bsl["find_path"]("Точка", "НизкийМетод", from_hint="ОбщийБ")
    assert miss["found"] is False


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_budget_exceeded(monkeypatch, built):
    bsl, *_ = built
    monkeypatch.setattr(_bh, "_HIERARCHY_VISITED_CAP", 1)
    res = bsl["find_path"]("Корень", "НизкийМетод")
    assert res["found"] is False
    assert res["_meta"]["budget_exceeded"] is True


def test_per_node_caller_truncation_is_inconclusive(monkeypatch, built):
    # НизкийМетод has 2 callers (Точка, Дёрнуть). With a 1-caller page, the page
    # that misses Дёрнуть must NOT yield a conclusive found=False: has_more=True
    # → budget_exceeded=True (the dropped caller might have reached `from`).
    bsl, *_ = built
    monkeypatch.setattr(_bh, "_FIND_PATH_NODE_LIMIT", 1)
    res = bsl["find_path"]("Дёрнуть", "НизкийМетод")
    assert res["found"] is False
    assert res["_meta"]["budget_exceeded"] is True


def test_full_page_finds_caller_that_truncation_would_drop(built):
    # Sanity: with the default page size, the same query DOES find the edge —
    # so the truncation above is genuinely about paging, not an unreachable pair.
    bsl, *_ = built
    res = bsl["find_path"]("Дёрнуть", "НизкийМетод")
    assert res["found"] is True


# ---------------------------------------------------------------------------
# include_triggers in FS/no-index mode → key always present (=[])
# ---------------------------------------------------------------------------


def test_hierarchy_triggers_empty_list_in_fs_mode(project):
    bsl = _fs(project)  # no idx_reader
    res = bsl["find_call_hierarchy"]("Корень", depth=1, include_triggers=True)
    assert res["tree"]
    for node in res["tree"]:
        assert node.get("triggers") == []


def test_find_path_triggers_empty_list_in_fs_mode(project):
    bsl = _fs(project)  # no idx_reader
    res = bsl["find_path"]("Корень", "НизкийМетод", include_triggers=True)
    assert res["found"] is True
    for el in res["path"]:
        assert el.get("triggers") == []


# ---------------------------------------------------------------------------
# get_callers edge_exact unit
# ---------------------------------------------------------------------------


def test_get_callers_edge_exact_flag(built):
    _bsl, reader, _base, _db = built
    res = reader.get_callers("НизкийМетод")
    by_name = {c["caller_name"]: c for c in res["callers"]}
    # Qualified ОбщийА.Точка → resolved callee_key → edge_exact True.
    assert by_name["Точка"]["edge_exact"] is True
    # Bare cross-module НизкийМетод() in Прямой.Дёрнуть → callee_key NULL → recall.
    assert by_name["Дёрнуть"]["edge_exact"] is False


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def test_reg_sig_and_recipe(project):
    helpers, resolve_safe = make_helpers(str(project))
    fmt = detect_format(str(project))
    bsl = make_bsl_helpers(
        base_path=str(project),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
    )
    entry = bsl["_registry"]["find_path"]
    sig = entry["sig"]
    for token in ("find_path", "max_depth", "from_hint", "to_hint", "precision", "call_line"):
        assert token in sig
    recipe = entry["recipe"]
    assert "budget_exceeded" in recipe
    assert "precision" in recipe
