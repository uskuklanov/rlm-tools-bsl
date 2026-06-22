"""Tests for find_path — reachability over the call graph (v1.19.0, step 4a).

Also covers the additive per-edge ``edge_exact`` flag added to
``IndexReader.get_callers`` (the basis of find_path's ``precision``).
"""

import shutil
import sqlite3

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


# Document Тест defines `Дубль` in BOTH its ObjectModule AND ManagerModule —
# same object_name, two distinct rel_paths (the real-world same-name-in-one-object
# case). ДубльВерх.Корень makes a real qualified call so the call graph is
# non-empty (has_calls=True → guard active).
DUP_OBJECT_BSL = """\
Процедура Дубль() Экспорт
    Сообщить("объект");
КонецПроцедуры
"""

DUP_MANAGER_BSL = """\
Процедура Дубль() Экспорт
    Сообщить("менеджер");
КонецПроцедуры
"""

DUP_VERH_BSL = """\
Процедура Корень() Экспорт
    ДубльОбщий.Тронуть();
КонецПроцедуры
"""

DUP_OBSHIY_BSL = """\
Процедура Тронуть() Экспорт
    Сообщить("тронул");
КонецПроцедуры
"""

DUP_FILES = {
    "Documents/Тест/Ext/ObjectModule.bsl": DUP_OBJECT_BSL,
    "Documents/Тест/Ext/ManagerModule.bsl": DUP_MANAGER_BSL,
    "CommonModules/ДубльВерх/Ext/Module.bsl": DUP_VERH_BSL,
    "CommonModules/ДубльОбщий/Ext/Module.bsl": DUP_OBSHIY_BSL,
}


def _write_project(root):
    for rel, content in PROJECT_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
    (root / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")


def _write_dup_project(root):
    for rel, content in DUP_FILES.items():
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


def _make_bsl(base, reader, *, authoritative=True):
    """Build helpers around an EXISTING reader (or proxy), with an explicit
    idx_zero_callers_authoritative flag — for the ambiguity-guard precondition tests."""
    helpers, resolve_safe = make_helpers(str(base))
    fmt = detect_format(str(base))
    return make_bsl_helpers(
        base_path=str(base),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
        idx_reader=reader,
        idx_zero_callers_authoritative=authoritative,
    )


_RAISE = object()  # sentinel: a proxy attr that raises AttributeError on access


class _ReaderProxy:
    """Delegates every attribute to a real reader, but lets a test override one
    attr — a value, a callable, or the ``_RAISE`` sentinel (raises AttributeError).
    Used to disable/break ``sample_method_definitions`` / ``has_calls`` without
    touching the real reader."""

    def __init__(self, real, **overrides):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            val = overrides[name]
            if val is _RAISE:
                raise AttributeError(name)
            return val
        return getattr(object.__getattribute__(self, "_real"), name)


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


@pytest.fixture
def built_dup(tmp_path):
    _write_dup_project(tmp_path)
    db_path = IndexBuilder().build(str(tmp_path), build_calls=True)
    bsl, reader = _indexed(tmp_path, db_path)
    yield bsl, reader, str(tmp_path), db_path
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
    # v1.25.0 ambiguity guard: the agent-facing surface must teach the
    # {error, hint, candidates} contract for a multi-defined name without a hint.
    surface = sig + recipe
    for token in ("error", "hint", "candidates"):
        assert token in surface, f"{token!r} missing from find_path sig/recipe"


# ---------------------------------------------------------------------------
# Ambiguity guard (v1.25.0) — multi-defined name without a matching hint bails
# to {error, hint, candidates} instead of a pathological reverse-BFS walk.
# ---------------------------------------------------------------------------


def test_ambiguous_to_without_hint_returns_hint_error(built):
    # "Точка" is defined in BOTH ОбщийА and ОбщийБ → ambiguous; "Корень" is unique.
    # to is checked first → ambiguous_arg == "to".
    bsl, *_ = built
    res = bsl["find_path"]("Корень", "Точка")
    assert "error" in res
    assert res["found"] is False
    assert res["path"] is None
    assert res["_meta"]["ambiguous_arg"] == "to"
    assert res["_meta"]["ambiguous"] is True
    assert res["_meta"]["definition_count"] == 2
    cands = res["candidates"]
    assert cands, "candidates must not be empty"
    for c in cands:
        assert c["file"]
        assert isinstance(c["line"], int)


def test_ambiguous_from_without_hint_returns_hint_error(built):
    # to ("НизкийМетод") is unique → not guarded; from ("Точка") is ambiguous.
    bsl, *_ = built
    res = bsl["find_path"]("Точка", "НизкийМетод")
    assert "error" in res
    assert res["found"] is False
    assert res["_meta"]["ambiguous_arg"] == "from"


def test_ambiguous_with_hint_resolves(built):
    # A matching from_hint pins the ambiguous end → guard does NOT fire, normal walk.
    bsl, *_ = built
    res = bsl["find_path"]("Точка", "НизкийМетод", from_hint="ОбщийА")
    assert "error" not in res
    assert res["found"] is True


def test_unique_names_no_ambiguity_error(built):
    # Regression anchor: both ends unique → guard silent, normal reachability.
    bsl, *_ = built
    res = bsl["find_path"]("Корень", "НизкийМетод")
    assert "error" not in res
    assert res["found"] is True


def test_self_path_ambiguous_not_broken(built_dup):
    # `Дубль` is defined in BOTH ObjectModule and ManagerModule of document Тест
    # (same object_name, two rel_paths).
    bsl, *_ = built_dup
    # (a) trivial self-path (from==to, no hints) → guard skipped, found True.
    self_res = bsl["find_path"]("Дубль", "Дубль")
    assert "error" not in self_res
    assert self_res["found"] is True
    # (b) ambiguous `to` with no hint → error; candidates are TWO distinct files
    # sharing the same object_name.
    res = bsl["find_path"]("Корень", "Дубль")
    assert "error" in res
    cands = res["candidates"]
    files = {c["file"] for c in cands}
    assert len(files) == 2
    assert len({c["object_name"] for c in cands}) == 1


def test_self_name_one_sided_hint_guards_other_end(built):
    # Narrow self-exclusion: from==to but ONE hint given → the OTHER (hintless)
    # end is still guarded for ambiguity.
    bsl, *_ = built
    res_to = bsl["find_path"]("Точка", "Точка", from_hint="ОбщийА")
    assert "error" in res_to
    assert res_to["_meta"]["ambiguous_arg"] == "to"
    res_from = bsl["find_path"]("Точка", "Точка", to_hint="ОбщийА")
    assert "error" in res_from
    assert res_from["_meta"]["ambiguous_arg"] == "from"


def test_reader_sample_method_definitions(built):
    _bsl, reader, *_ = built
    # Multi-defined name, default limit → total via len(rows) (no separate COUNT).
    s = reader.sample_method_definitions("Точка")
    assert s["total"] == 2
    assert len(s["candidates"]) == 2
    for c in s["candidates"]:
        assert c["file"]
        assert isinstance(c["line"], int)
        assert "object_name" in c and "category" in c and "module_type" in c
    # Truncated branch: limit=1 forces the separate COUNT(*) path.
    s1 = reader.sample_method_definitions("Точка", limit=1)
    assert s1["total"] == 2
    assert len(s1["candidates"]) == 1
    # Unique / missing names.
    assert reader.sample_method_definitions("НизкийМетод")["total"] == 1
    assert reader.sample_method_definitions("НетТакого")["total"] == 0
    # NOCASE does NOT fold Cyrillic case — a lowercase Cyrillic input misses the
    # canonical PascalCase name (deliberate contract: guard never pays py_lower).
    assert reader.sample_method_definitions("точка")["total"] == 0


def test_sample_method_definitions_none_on_broken_table(built, tmp_path):
    # Distinguishes "index broken" (None) from "no such method" (total=0), like
    # get_definitions. Drop the methods table on a copy → OperationalError → None.
    _bsl, _reader, _base, db_path = built
    broken = tmp_path / "broken_index.db"
    shutil.copy(db_path, broken)
    con = sqlite3.connect(str(broken))
    con.execute("DROP TABLE methods")
    con.commit()
    con.close()
    r2 = IndexReader(str(broken))
    try:
        assert r2.sample_method_definitions("Точка") is None
    finally:
        r2.close()


def test_soft_capability_disables_guard(built):
    # Old/duck-typed reader without a usable sample_method_definitions → guard OFF
    # (no AttributeError on the missing method, no crash on a non-dict return).
    bsl, reader, base, _db = built
    missing = _make_bsl(base, _ReaderProxy(reader, sample_method_definitions=_RAISE))
    res_missing = missing["find_path"]("Точка", "НизкийМетод")
    assert "error" not in res_missing
    assert "found" in res_missing

    nondict = _make_bsl(base, _ReaderProxy(reader, sample_method_definitions=lambda *a, **k: object()))
    res_nondict = nondict["find_path"]("Точка", "НизкийМетод")
    assert "error" not in res_nondict
    assert "found" in res_nondict


def test_no_calls_index_disables_guard(built):
    # Precondition has_calls: without a call graph the methods probe is unreliable
    # and find_path falls back to FS anyway → guard must not fire.
    bsl, reader, base, _db = built
    no_calls = _make_bsl(base, _ReaderProxy(reader, has_calls=False))
    res = no_calls["find_path"]("Точка", "НизкийМетод")
    assert "error" not in res


def test_non_authoritative_index_disables_guard(built):
    # Precondition idx_zero_callers_authoritative: on a stale/non-authoritative
    # index the guard stays out of the way (find_path uses the FS fallback).
    bsl, reader, base, _db = built
    stale = _make_bsl(base, reader, authoritative=False)
    res = stale["find_path"]("Корень", "Точка")
    assert "error" not in res


def test_ambiguous_bail_skips_resolve_and_pylower(monkeypatch, built):
    # The guard sits BEFORE resolve_target_identity, so an ambiguous no-hint bail
    # never pays the py_lower SCAN of _resolve_target_key.
    bsl, reader, *_ = built
    calls = []
    orig = reader.resolve_target_identity

    def _spy(name, hint=""):
        calls.append((name, hint))
        return orig(name, hint)

    monkeypatch.setattr(reader, "resolve_target_identity", _spy)
    res = bsl["find_path"]("Корень", "Точка")
    assert "error" in res
    assert calls == [], "resolve_target_identity must NOT run on an ambiguous bail"
