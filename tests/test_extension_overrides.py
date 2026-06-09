"""Tests for extension_overrides indexing (v1.5.0, Level-8).

Covers: schema, collector, build/update, IndexReader, helpers enrichment.
"""

import os
import sqlite3
import textwrap

import pytest

from rlm_tools_bsl.bsl_index import BUILDER_VERSION, IndexBuilder, IndexReader


# ---------------------------------------------------------------------------
# Helpers to create test fixtures
# ---------------------------------------------------------------------------

_CF_MAIN_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Configuration uuid="00000000-0000-0000-0000-000000000001">
            <Properties>
                <Name>ОсновнаяКонфигурация</Name>
                <NamePrefix/>
                <ConfigurationExtensionCompatibilityMode>Version8_3_24</ConfigurationExtensionCompatibilityMode>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")


def _cf_extension_xml(name="ТестовоеРасширение", purpose="Customization", prefix="мр_"):
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                        xmlns:v8="http://v8.1c.ru/8.1/data/core">
            <Configuration uuid="00000000-0000-0000-0000-000000000002">
                <Properties>
                    <ObjectBelonging>Adopted</ObjectBelonging>
                    <Name>{name}</Name>
                    <ConfigurationExtensionPurpose>{purpose}</ConfigurationExtensionPurpose>
                    <NamePrefix>{prefix}</NamePrefix>
                </Properties>
            </Configuration>
        </MetaDataObject>
    """)


_MAIN_MODULE_BSL = textwrap.dedent("""\
    Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)
        // основная логика
    КонецПроцедуры

    Процедура ПередЗаписью(Отказ)
        // валидация
    КонецПроцедуры
""")


_EXT_MODULE_BSL = textwrap.dedent("""\
    &После("ОбработкаЗаполнения")
    Процедура мр_ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)
        // расширенная логика
    КонецПроцедуры

    &Вместо("ПередЗаписью")
    Процедура мр_ПередЗаписью(Отказ)
        // замена
    КонецПроцедуры
""")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_main_with_extension(parent_dir):
    """Create src/cf/ (main) + src/cfe/ТестовоеРасширение/ (extension)."""
    cf = os.path.join(parent_dir, "src", "cf")
    cfe = os.path.join(parent_dir, "src", "cfe", "ТестовоеРасширение")

    # Main config
    _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
    _write(
        os.path.join(cf, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
        _MAIN_MODULE_BSL,
    )

    # Extension
    _write(os.path.join(cfe, "Configuration.xml"), _cf_extension_xml())
    _write(
        os.path.join(cfe, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
        _EXT_MODULE_BSL,
    )

    return cf, cfe


def _make_extension_only(parent_dir):
    """Create standalone extension directory."""
    ext_dir = os.path.join(parent_dir, "ext")
    _write(os.path.join(ext_dir, "Configuration.xml"), _cf_extension_xml())
    _write(
        os.path.join(ext_dir, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
        _EXT_MODULE_BSL,
    )
    return ext_dir


# ---------------------------------------------------------------------------
# Schema and version
# ---------------------------------------------------------------------------


class TestSchema:
    def test_builder_version_is_10(self):
        assert BUILDER_VERSION == 14

    def test_extension_overrides_table_created(self, tmp_path, monkeypatch):
        """Build creates extension_overrides table in schema."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        # Table must exist
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "extension_overrides" in tables


# ---------------------------------------------------------------------------
# Build with extensions (main config)
# ---------------------------------------------------------------------------


class TestBuildMainWithExtensions:
    def test_build_populates_overrides(self, tmp_path, monkeypatch):
        """Build main config with nearby extension populates extension_overrides."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()
        conn.close()

        assert len(rows) == 2

        # Check override data
        overrides = [dict(r) for r in rows]
        by_method = {ov["target_method"]: ov for ov in overrides}

        assert "ОбработкаЗаполнения" in by_method
        assert "ПередЗаписью" in by_method

        ov1 = by_method["ОбработкаЗаполнения"]
        assert ov1["annotation"] == "После"
        assert ov1["extension_name"] == "ТестовоеРасширение"
        assert ov1["extension_method"] == "мр_ОбработкаЗаполнения"
        assert ov1["object_name"] == "Номенклатура"
        assert ov1["extension_root"] == cfe
        assert ov1["ext_module_path"] == "Catalogs/Номенклатура/Ext/ObjectModule.bsl"

        ov2 = by_method["ПередЗаписью"]
        assert ov2["annotation"] == "Вместо"

    def test_source_module_linked(self, tmp_path, monkeypatch):
        """Overrides link to source module via source_module_id and source_path."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()

        for r in rows:
            assert r["source_module_id"] is not None
            assert r["source_path"] != ""
            assert "Номенклатура" in r["source_path"]

        conn.close()

    def test_target_method_line_populated(self, tmp_path, monkeypatch):
        """target_method_line is populated from methods table."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()

        for r in rows:
            assert r["target_method_line"] is not None
            assert r["target_method_line"] > 0

        conn.close()

    def test_meta_written(self, tmp_path, monkeypatch):
        """has_extension_overrides and extension_overrides_count in meta."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        has = conn.execute("SELECT value FROM index_meta WHERE key='has_extension_overrides'").fetchone()[0]
        count = conn.execute("SELECT value FROM index_meta WHERE key='extension_overrides_count'").fetchone()[0]
        conn.close()

        assert has == "1"
        assert int(count) == 2

    def test_no_extensions_meta_zero(self, tmp_path, monkeypatch):
        """Config without extensions writes meta with 0."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        has = conn.execute("SELECT value FROM index_meta WHERE key='has_extension_overrides'").fetchone()[0]
        count = conn.execute("SELECT value FROM index_meta WHERE key='extension_overrides_count'").fetchone()[0]
        conn.close()

        assert has == "0"
        assert count == "0"


# ---------------------------------------------------------------------------
# Build extension-only (no main config)
# ---------------------------------------------------------------------------


class TestBuildExtensionOnly:
    def test_extension_build_no_source_link(self, tmp_path, monkeypatch):
        """Building an extension without main config: source_module_id=NULL."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        ext_dir = _make_extension_only(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(ext_dir, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()
        conn.close()

        assert len(rows) == 2
        for r in rows:
            assert r["source_module_id"] is None
            assert r["source_path"] == ""
            assert r["target_method_line"] is None


class TestLiveOverridesContract:
    """get_overrides() LIVE fallback must carry extension_name in EVERY branch.

    Regression: a session opened directly ON an extension (role=EXTENSION) used the
    raw find_extension_overrides rows, which lack extension_name → the `расширения`
    recipe's `{o['extension_name'] for o in ov}` raised KeyError (confirmed live).
    """

    def test_extension_session_live_overrides_have_extension_name(self, tmp_path):
        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
        from rlm_tools_bsl.format_detector import detect_format
        from rlm_tools_bsl.helpers import make_helpers

        ext_dir = _make_extension_only(tmp_path)
        helpers, resolve_safe = make_helpers(str(ext_dir))
        fmt = detect_format(str(ext_dir))
        bsl = make_bsl_helpers(
            base_path=str(ext_dir),
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=fmt,
            idx_reader=None,  # force the live fallback → EXTENSION-role branch
        )
        res = bsl["get_overrides"]()
        assert res["source"] == "live"
        assert res["overrides"], "expected live overrides from the extension"
        for ov in res["overrides"]:
            assert ov.get("extension_name"), ov
            assert "extension_root" in ov
        # The расширения recipe's set-comprehension must not raise.
        names = {o["extension_name"] for o in res["overrides"]}
        assert names == {"ТестовоеРасширение"}


# ---------------------------------------------------------------------------
# IndexReader methods
# ---------------------------------------------------------------------------


class TestIndexReaderOverrides:
    @pytest.fixture()
    def reader(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)
        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)
        r = IndexReader(db_path)
        yield r
        r.close()

    def test_get_extension_overrides_all(self, reader):
        result = reader.get_extension_overrides()
        assert result is not None
        assert len(result) == 2

    def test_get_extension_overrides_by_object(self, reader):
        result = reader.get_extension_overrides(object_name="Номенклатура")
        assert result is not None
        assert len(result) == 2

    def test_get_extension_overrides_by_method(self, reader):
        result = reader.get_extension_overrides(method_name="ОбработкаЗаполнения")
        assert result is not None
        assert len(result) == 1
        assert result[0]["annotation"] == "После"

    def test_get_extension_overrides_empty_filter(self, reader):
        result = reader.get_extension_overrides(object_name="НесуществующийОбъект")
        assert result is not None
        assert len(result) == 0

    def test_get_overrides_for_path(self, reader):
        result = reader.get_overrides_for_path("Catalogs/Номенклатура/Ext/ObjectModule.bsl")
        assert isinstance(result, dict)
        assert "ОбработкаЗаполнения" in result
        assert "ПередЗаписью" in result
        assert len(result["ОбработкаЗаполнения"]) == 1

    def test_get_overrides_for_path_no_match(self, reader):
        result = reader.get_overrides_for_path("NonExistent/Path.bsl")
        assert result == {}

    def test_get_extension_overrides_grouped(self, reader, tmp_path):
        cf = str(tmp_path / "src" / "cf")
        result = reader.get_extension_overrides_grouped(base_path=cf)
        assert result is not None
        assert len(result) > 0
        # All overrides grouped under extension_root key
        total = sum(len(v) for v in result.values())
        assert total == 2

    def test_statistics_includes_overrides(self, reader):
        stats = reader.get_statistics()
        assert "extension_overrides" in stats
        assert stats["extension_overrides"] == 2


# ---------------------------------------------------------------------------
# Backward compatibility: pre-v9 index
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_reader_methods_return_none_on_missing_table(self, tmp_path, monkeypatch):
        """Pre-v9 index without extension_overrides table: methods return None/{}."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        # Simulate pre-v9: drop the table
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS extension_overrides")
        conn.commit()
        conn.close()

        reader = IndexReader(db_path)
        assert reader.get_extension_overrides() is None
        assert reader.get_overrides_for_path("any/path.bsl") == {}
        assert reader.get_extension_overrides_grouped() is None
        reader.close()


# ---------------------------------------------------------------------------
# Update (incremental)
# ---------------------------------------------------------------------------


class TestUpdateOverrides:
    def test_update_refreshes_overrides(self, tmp_path, monkeypatch):
        """update() refreshes extension_overrides table."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, cfe = _make_main_with_extension(tmp_path)

        builder = IndexBuilder()
        builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        # Update (no file changes, but overrides re-scanned)
        builder.update(cf)

        # Verify overrides still present
        from rlm_tools_bsl.bsl_index import get_index_db_path

        db_path = get_index_db_path(cf)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM extension_overrides").fetchone()[0]
        conn.close()
        assert count == 2

    def test_update_soft_upgrade_from_v8(self, tmp_path, monkeypatch):
        """update() creates extension_overrides table on v8 index."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(os.path.join(cf, "CommonModules", "Test", "Ext", "Module.bsl"), "")

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        # Simulate v8: drop table
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS extension_overrides")
        conn.commit()
        conn.close()

        # update() should re-create it
        builder.update(cf)

        conn = sqlite3.connect(str(db_path))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "extension_overrides" in tables


# ---------------------------------------------------------------------------
# Self-mapping: extension grouped key
# ---------------------------------------------------------------------------


class TestSelfMapping:
    def test_extension_grouped_self_key(self, tmp_path, monkeypatch):
        """For extension config, extension_root == base_path -> key 'self'."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        ext_dir = _make_extension_only(tmp_path)

        builder = IndexBuilder()
        db_path = builder.build(ext_dir, build_calls=False, build_metadata=False, build_fts=False)

        reader = IndexReader(db_path)
        grouped = reader.get_extension_overrides_grouped(base_path=ext_dir)
        reader.close()

        assert grouped is not None
        assert "self" in grouped
        assert len(grouped["self"]) == 2


# ---------------------------------------------------------------------------
# Fix 1: case-insensitive Cyrillic matching
# ---------------------------------------------------------------------------


_EXT_MODULE_LOWERCASE = textwrap.dedent("""\
    &После("обработказаполнения")
    Процедура мр_ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка)
        // расширенная логика
    КонецПроцедуры
""")


class TestCaseInsensitiveCyrillic:
    def test_method_line_found_with_case_mismatch(self, tmp_path, monkeypatch):
        """target_method_line resolved even when annotation has different case."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ТестовоеРасширение")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(
            os.path.join(cf, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
            _MAIN_MODULE_BSL,
        )
        _write(os.path.join(cfe, "Configuration.xml"), _cf_extension_xml())
        _write(
            os.path.join(cfe, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
            _EXT_MODULE_LOWERCASE,
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM extension_overrides").fetchall()
        conn.close()

        assert len(rows) == 1
        # target_method_line must be found despite case mismatch
        assert rows[0]["target_method_line"] is not None
        assert rows[0]["target_method_line"] > 0

    def test_reader_filter_case_insensitive(self, tmp_path, monkeypatch):
        """get_extension_overrides filters case-insensitively for Cyrillic."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf, _cfe = _make_main_with_extension(tmp_path)
        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        reader = IndexReader(db_path)
        # Query with different case
        result = reader.get_extension_overrides(method_name="обработказаполнения")
        reader.close()

        assert result is not None
        assert len(result) == 1
        assert result[0]["target_method"] == "ОбработкаЗаполнения"

    def test_overrides_for_path_case_insensitive(self, tmp_path, monkeypatch):
        """get_overrides_for_path groups are matched case-insensitively by extract_procedures."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = os.path.join(str(tmp_path), "src", "cf")
        cfe = os.path.join(str(tmp_path), "src", "cfe", "ТестовоеРасширение")

        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
        _write(
            os.path.join(cf, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
            _MAIN_MODULE_BSL,
        )
        _write(os.path.join(cfe, "Configuration.xml"), _cf_extension_xml())
        _write(
            os.path.join(cfe, "Catalogs", "Номенклатура", "Ext", "ObjectModule.bsl"),
            _EXT_MODULE_LOWERCASE,
        )

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        reader = IndexReader(db_path)
        result = reader.get_overrides_for_path("Catalogs/Номенклатура/Ext/ObjectModule.bsl")
        reader.close()

        # Key in the dict is "обработказаполнения" (from annotation), but
        # extract_procedures uses .lower() matching, so it should still work
        assert len(result) == 1
        # The single key should contain the override
        all_overrides = [ov for ovs in result.values() for ov in ovs]
        assert len(all_overrides) == 1


# ---------------------------------------------------------------------------
# Fix 3: early-exit build without BSL writes meta keys
# ---------------------------------------------------------------------------


class TestEarlyExitMeta:
    def test_build_no_bsl_writes_override_meta(self, tmp_path, monkeypatch):
        """Build with no .bsl files still writes has_extension_overrides meta."""
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        cf = str(tmp_path / "cf")
        # Only Configuration.xml, no BSL files
        _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)

        builder = IndexBuilder()
        db_path = builder.build(cf, build_calls=False, build_metadata=False, build_fts=False)

        conn = sqlite3.connect(str(db_path))
        has = conn.execute("SELECT value FROM index_meta WHERE key='has_extension_overrides'").fetchone()
        count = conn.execute("SELECT value FROM index_meta WHERE key='extension_overrides_count'").fetchone()
        conn.close()

        assert has is not None, "has_extension_overrides must be in meta"
        assert has[0] == "0"
        assert count is not None, "extension_overrides_count must be in meta"
        assert count[0] == "0"
