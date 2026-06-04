"""Tests for the call-graph noise filter (bare reserved platform globals).

The extractor drops bare/unqualified calls to reserved 1C platform globals
(``Сообщить`` / ``НСтр`` / ``Новый Структура(`` / ``НачатьТранзакцию`` / …)
because they can never resolve to a real method in the configuration — they are
permanently unresolved navigation noise (~27% of the whole ``calls`` table on
real configs). The filter is applied ONLY to bare calls, never to the qualified
``Модуль.Метод(`` branch (a same-named CommonModule export could be real).

Two mechanisms are covered:
  * extraction-time filter — new builds never store the noise (CI units 1–5);
  * one-time in-place purge — legacy indexes built before the filter are cleaned
    on the first ``update()`` via ``_purge_call_noise_once`` (CI unit 6).

A 7th opt-in test ports the recon "oracle" against real client indexes: it must
remove ~27–28% of the table while deleting ZERO resolved edges. It is skipped
unless both ``RLM_VALIDATION_INDEXES`` (CSV of base_paths) and
``RLM_VALIDATION_INDEX_DIR`` (real index root) are set.
"""

import os
import sqlite3

import pytest

from rlm_tools_bsl.bsl_index import (
    _BSL_GLOBAL_FUNCS_LOWER,
    IndexBuilder,
    _make_callee_key,
    get_index_db_path,
)

# ---------------------------------------------------------------------------
# Synthetic fixture project
# ---------------------------------------------------------------------------

# Bare reserved globals (filtered) sitting next to a real local call (kept).
ENTRY_MODULE_BSL = """\
Процедура ТочкаВхода() Экспорт
    Сообщить("x");
    НСтр("ru = 'y'");
    Формат(123, "ЧЦ=2");
    НачатьТранзакцию();
    Стр = Новый Структура("а", 1);
    МойХелпер();
КонецПроцедуры

Процедура МойХелпер()
    Возврат;
КонецПроцедуры
"""

# Collision guard: ЗакрытьФорму is a REAL user method here (deliberately NOT in
# the filter set). The bare call must survive extraction AND resolve locally.
COLLISION_MODULE_BSL = """\
Процедура Обработчик() Экспорт
    ЗакрытьФорму();
КонецПроцедуры

Процедура ЗакрытьФорму()
    Возврат;
КонецПроцедуры
"""

# Qualified call whose method part is a "global" name — the qualified branch is
# never filtered, so the edge ОбщийМодуль.Формат must be kept.
QUALIFIED_MODULE_BSL = """\
Процедура Точка() Экспорт
    ОбщийМодуль.Формат(123);
КонецПроцедуры
"""

ENTRY_PATH = "CommonModules/Тест/Ext/Module.bsl"
COLLISION_PATH = "CommonModules/Форма1/Ext/Module.bsl"
QUALIFIED_PATH = "CommonModules/Вызыватель/Ext/Module.bsl"

PROJECT_FILES = {
    ENTRY_PATH: ENTRY_MODULE_BSL,
    COLLISION_PATH: COLLISION_MODULE_BSL,
    QUALIFIED_PATH: QUALIFIED_MODULE_BSL,
}


def _write_project(root, files):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8-sig")
    (root / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    _write_project(tmp_path, PROJECT_FILES)
    return tmp_path


@pytest.fixture
def built(project):
    db_path = IndexBuilder().build(str(project), build_calls=True)
    return db_path, str(project)


def _conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


def _callees(db_path):
    with _conn(db_path) as c:
        return {r["callee_name"] for r in c.execute("SELECT DISTINCT callee_name FROM calls")}


def _edge_key(db_path, caller_rel, callee_name):
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT c.callee_key FROM calls c "
            "JOIN methods m ON m.id = c.caller_id "
            "JOIN modules mod ON mod.id = m.module_id "
            "WHERE mod.rel_path = ? AND c.callee_name = ?",
            (caller_rel, callee_name),
        ).fetchone()
    return row["callee_key"] if row else "<<missing>>"


# ===========================================================================
# CI units — extraction-time filter (always run)
# ===========================================================================


def test_bare_globals_excluded(built):
    """Bare reserved globals never land in the calls table."""
    db_path, _ = built
    callees = _callees(db_path)
    for noise in ("Сообщить", "НСтр", "Формат", "НачатьТранзакцию", "Структура"):
        assert noise not in callees, f"reserved global leaked as a call edge: {noise}"


def test_real_calls_preserved(built):
    """A real local call right next to the filtered noise is kept."""
    db_path, _ = built
    assert "МойХелпер" in _callees(db_path)


def test_collision_name_kept(built):
    """ЗакрытьФорму is a real user method → edge kept AND resolved locally.

    Guards against anyone adding a collision name to the filter set.
    """
    db_path, _ = built
    key = _edge_key(db_path, COLLISION_PATH, "ЗакрытьФорму")
    assert key == _make_callee_key(COLLISION_PATH, "ЗакрытьФорму")


def test_qualified_same_name_kept(built):
    """The qualified branch is never filtered — ОбщийМодуль.Формат survives."""
    db_path, _ = built
    callees = _callees(db_path)
    assert "ОбщийМодуль.Формат" in callees
    # …and the bare form is NOT present (it was the method part of the qualified
    # call, and Формат is a filtered global anyway).
    assert "Формат" not in callees


def test_curated_set_guard():
    """Direct guard on the curated set: size, exclusions, anchors."""
    assert len(_BSL_GLOBAL_FUNCS_LOWER) == 105
    # The collision names must NEVER be in the set — each is a real user method in
    # some production config (proven by the 5-base oracle: removed_resolved>0).
    # Квартал is shadowed by a real method in a БГУ (budget-accounting) config.
    for excluded in (
        "ЗакрытьФорму",
        "ПолеКомпоновкиДанных",
        "ТаблицаЗначений",
        "УникальныйИдентификатор",
        "Массив",
        "Квартал",
    ):
        assert excluded.casefold() not in _BSL_GLOBAL_FUNCS_LOWER, excluded
    # Anchors that must be present.
    for anchor in ("Сообщить", "НачатьТранзакцию", "Формат", "НСтр"):
        assert anchor.casefold() in _BSL_GLOBAL_FUNCS_LOWER, anchor


# ===========================================================================
# CI unit — one-time in-place purge of legacy indexes (always run)
# ===========================================================================


def _aggregates_match_live(db_path):
    with _conn(db_path) as c:
        total = c.execute("SELECT value FROM index_meta WHERE key='calls_total'").fetchone()
        resolved = c.execute("SELECT value FROM index_meta WHERE key='calls_resolved'").fetchone()
        live_total = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        live_resolved = c.execute("SELECT COUNT(callee_key) FROM calls").fetchone()[0]
        flag = c.execute("SELECT value FROM index_meta WHERE key='calls_noise_cleaned'").fetchone()
    assert total is not None and int(total["value"]) == live_total
    assert resolved is not None and int(resolved["value"]) == live_resolved
    return flag


def test_inplace_cleanup_idempotent(built):
    """Legacy index (flag cleared, noise injected) is purged once on update()."""
    db_path, base_path = built

    # Simulate a legacy index: inject an old noise row + a control resolved edge
    # + a resolved row whose NAME is a filtered global (must survive — the purge
    # guard is callee_key IS NULL), then clear the cleaned flag.
    with _conn(db_path) as c:
        caller_id = c.execute("SELECT id FROM methods LIMIT 1").fetchone()["id"]
        c.execute(
            "INSERT INTO calls (caller_id, callee_name, line, callee_key) VALUES (?, ?, ?, ?)",
            (caller_id, "Сообщить", 1, None),  # bare global, NULL → must be deleted
        )
        c.execute(
            "INSERT INTO calls (caller_id, callee_name, line, callee_key) VALUES (?, ?, ?, ?)",
            (caller_id, "КонтрольныйВызов", 2, "ctrl::контрольныйвызов"),  # non-noise → kept
        )
        c.execute(
            "INSERT INTO calls (caller_id, callee_name, line, callee_key) VALUES (?, ?, ?, ?)",
            (caller_id, "Формат", 3, "ctrl::формат"),  # noise NAME but RESOLVED → guard keeps it
        )
        c.execute("DELETE FROM index_meta WHERE key = 'calls_noise_cleaned'")
        c.commit()

    # First update() → purge fires.
    IndexBuilder().update(base_path)

    callees = _callees(db_path)
    assert "Сообщить" not in callees, "injected noise row must be purged"
    # Control non-noise edge survives.
    assert "КонтрольныйВызов" in callees
    # Resolved row with a filtered NAME survives (callee_key IS NOT NULL guard).
    with _conn(db_path) as c:
        kept = c.execute("SELECT COUNT(*) FROM calls WHERE callee_name='Формат' AND callee_key IS NOT NULL").fetchone()[
            0
        ]
    assert kept == 1, "a RESOLVED row must never be deleted, even with a filtered name"
    # Collision method from the build is intact.
    assert _edge_key(db_path, COLLISION_PATH, "ЗакрытьФорму") == _make_callee_key(COLLISION_PATH, "ЗакрытьФорму")
    flag = _aggregates_match_live(db_path)
    assert flag is not None and flag["value"] == "1"

    with _conn(db_path) as c:
        total_after_first = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]

    # Second (no-op) update() → purge already done, table unchanged, meta in sync.
    IndexBuilder().update(base_path)

    with _conn(db_path) as c:
        total_after_second = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    assert total_after_second == total_after_first, "idempotent: nothing more to delete"
    assert "Сообщить" not in _callees(db_path)
    flag = _aggregates_match_live(db_path)
    assert flag is not None and flag["value"] == "1"


# ===========================================================================
# Opt-in oracle — real client indexes (skipped without RLM_VALIDATION_INDEXES)
# ===========================================================================


def _oracle_measure(db_path):
    """Read-only emulation of the filter against an EXISTING index.

    Returns (total, removed, removed_resolved). ``removed_resolved`` MUST be 0
    (the filter must never touch a row that resolved to a real method).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.create_function("cf", 1, lambda s: s.casefold() if s is not None else None, deterministic=True)
        names = list(_BSL_GLOBAL_FUNCS_LOWER)
        ph = ",".join("?" * len(names))
        total = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        removed = conn.execute(
            f"SELECT COUNT(*) FROM calls WHERE instr(callee_name, '.') = 0 AND cf(callee_name) IN ({ph})",
            names,
        ).fetchone()[0]
        removed_resolved = conn.execute(
            f"SELECT COUNT(*) FROM calls "
            f"WHERE instr(callee_name, '.') = 0 AND callee_key IS NOT NULL "
            f"AND cf(callee_name) IN ({ph})",
            names,
        ).fetchone()[0]
    finally:
        conn.close()
    return total, removed, removed_resolved


@pytest.mark.skipif(
    not (os.environ.get("RLM_VALIDATION_INDEXES") and os.environ.get("RLM_VALIDATION_INDEX_DIR")),
    reason=(
        "set RLM_VALIDATION_INDEXES (CSV of base_paths) + RLM_VALIDATION_INDEX_DIR "
        "(real index root) to run the real-index oracle"
    ),
)
def test_oracle_zero_loss_on_real_indexes(monkeypatch):
    """Per-base gate: ZERO resolved edges removed, ~27–28% of the table shrunk.

    Point your validation configs (ideally spanning ERP / документооборот / БГУ
    domains) at this via RLM_VALIDATION_INDEXES so the merge gate is real, not
    documentary.

    A dedicated ``RLM_VALIDATION_INDEX_DIR`` is used (not ``RLM_INDEX_DIR``)
    because the autouse ``_isolate_real_home`` fixture clobbers ``RLM_INDEX_DIR``
    with a tmp dir for every test. We restore the real root here via monkeypatch,
    which applies on top of the autouse setup so ``get_index_db_path`` resolves
    the real client DBs.
    """
    monkeypatch.setenv("RLM_INDEX_DIR", os.environ["RLM_VALIDATION_INDEX_DIR"])
    raw = os.environ["RLM_VALIDATION_INDEXES"]
    base_paths = [p.strip() for p in raw.split(",") if p.strip()]
    assert base_paths, "RLM_VALIDATION_INDEXES set but empty after parsing"

    checked = 0
    for base_path in base_paths:
        db = get_index_db_path(base_path)
        if not db.exists():
            pytest.fail(f"index DB not found for {base_path}: {db} (set RLM_VALIDATION_INDEX_DIR?)")
        total, removed, removed_resolved = _oracle_measure(db)
        assert total > 0, f"empty calls table for {base_path}"
        # SAFETY: not a single resolved (real) edge may be removed. The oracle
        # ignores callee_key for `removed` (it models the EXTRACTION filter, which
        # runs before resolution) — so removed_resolved>0 means a name in the set
        # is a real user method in this config and must be excluded.
        assert removed_resolved == 0, f"{base_path}: filter would remove {removed_resolved} RESOLVED edges — collision!"
        pct = removed / total
        assert 0.24 <= pct <= 0.30, f"{base_path}: removal {pct:.1%} outside expected 24–30% band"
        checked += 1
    assert checked == len(base_paths)
