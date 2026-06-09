"""Tests for the unified 1C control-flow graph (v1.19.0, step 3).

``IndexReader.get_inbound_edges`` merges four non-call inbound-edge sources into
the SAME ``callee_key`` identity space used by the call graph:

  * ``cfe_override``  — extension CFE override that wraps the method;
  * ``form_event``    — a form-event handler that IS the method;
  * ``subscription``  — an EventSubscription handler;
  * ``scheduled_job`` — a ScheduledJob handler.

Plus the opt-in ``find_call_hierarchy(..., include_triggers=True)`` leaf annotation.
"""

import os
import sqlite3
import textwrap

import pytest

from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


# ---------------------------------------------------------------------------
# Fixture sources
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

_CF_EXT_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                    xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <Configuration uuid="00000000-0000-0000-0000-000000000002">
            <Properties>
                <ObjectBelonging>Adopted</ObjectBelonging>
                <Name>ТестовоеРасширение</Name>
                <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>
                <NamePrefix>рсш_</NamePrefix>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")

_DOC_OBJECT_BSL = textwrap.dedent("""\
    Процедура ОбработкаПроведения(Отказ, Режим)
        // проведение
    КонецПроцедуры

    Процедура ПередЗаписью(Отказ)
        // запись
    КонецПроцедуры
""")

_EXT_OBJECT_BSL = textwrap.dedent("""\
    &После("ОбработкаПроведения")
    Процедура рсш_ОбработкаПроведения(Отказ, Режим)
        // расширенная логика
    КонецПроцедуры
""")

_FORM_MODULE_BSL = textwrap.dedent("""\
    Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)
        // инициализация формы
    КонецПроцедуры
""")

_CF_FORM_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <Form xmlns="http://v8.1c.ru/8.3/xcf/logform">
      <Events>
        <Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>
      </Events>
    </Form>
""")

_CM_HANDLERS_BSL = textwrap.dedent("""\
    Процедура ПодпискаПриЗаписи(Источник, Отказ) Экспорт
        // обработчик подписки
    КонецПроцедуры
""")

_CM_JOBS_BSL = textwrap.dedent("""\
    Процедура ВыполнитьОбслуживание() Экспорт
        // регламентная операция
    КонецПроцедуры
""")


def _event_sub_xml(name, handler, source="cfg:DocumentObject.ТестовыйДокумент", event="OnWrite"):
    src = f"<Source><v8:Type>{source}</v8:Type></Source>" if source else ""
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                        xmlns:v8="http://v8.1c.ru/8.1/data/core">
        <EventSubscription>
        <Properties>
        <Name>{name}</Name>
        {src}
        <Handler>{handler}</Handler>
        <Event>{event}</Event>
        </Properties>
        </EventSubscription>
        </MetaDataObject>
    """)


_SCHEDULED_JOB_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core">
    <ScheduledJob>
    <Properties>
    <Name>Обслуживание</Name>
    <MethodName>CommonModule.РегламентныеОперации.ВыполнитьОбслуживание</MethodName>
    <Use>true</Use>
    </Properties>
    </ScheduledJob>
    </MetaDataObject>
""")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


@pytest.fixture
def built(tmp_path):
    """Main config (cf) + sibling extension (cfe); build full index."""
    cf = os.path.join(tmp_path, "src", "cf")
    cfe = os.path.join(tmp_path, "src", "cfe", "ТестовоеРасширение")

    _write(os.path.join(cf, "Configuration.xml"), _CF_MAIN_XML)
    _write(os.path.join(cf, "Documents", "ТестовыйДокумент", "Ext", "ObjectModule.bsl"), _DOC_OBJECT_BSL)
    _write(
        os.path.join(cf, "Documents", "ТестовыйДокумент", "Forms", "ФормаДокумента", "Ext", "Form.xml"),
        _CF_FORM_XML,
    )
    _write(
        os.path.join(cf, "Documents", "ТестовыйДокумент", "Forms", "ФормаДокумента", "Ext", "Form", "Module.bsl"),
        _FORM_MODULE_BSL,
    )
    _write(os.path.join(cf, "CommonModules", "ОбработчикиСобытий", "Ext", "Module.bsl"), _CM_HANDLERS_BSL)
    _write(os.path.join(cf, "CommonModules", "РегламентныеОперации", "Ext", "Module.bsl"), _CM_JOBS_BSL)
    _write(
        os.path.join(cf, "EventSubscriptions", "ПриЗаписиДок", "Ext", "EventSubscription.xml"),
        _event_sub_xml("ПриЗаписиДок", "CommonModule.ОбработчикиСобытий.ПодпискаПриЗаписи"),
    )
    # Recall case A: handler module that does NOT resolve in `modules`.
    _write(
        os.path.join(cf, "EventSubscriptions", "ОрфанПодписка", "Ext", "EventSubscription.xml"),
        _event_sub_xml("ОрфанПодписка", "CommonModule.НесуществующийМодуль.ОрфанОбработчик"),
    )
    # Recall case B: dotless handler → handler_module="" (R4 №2).
    _write(
        os.path.join(cf, "EventSubscriptions", "БезМодуля", "Ext", "EventSubscription.xml"),
        _event_sub_xml("БезМодуля", "ОбработчикБезМодуля"),
    )
    _write(os.path.join(cf, "ScheduledJobs", "Обслуживание", "Ext", "ScheduledJob.xml"), _SCHEDULED_JOB_XML)

    # Extension override of the document's ОбработкаПроведения.
    _write(os.path.join(cfe, "Configuration.xml"), _CF_EXT_XML)
    _write(os.path.join(cfe, "Documents", "ТестовыйДокумент", "Ext", "ObjectModule.bsl"), _EXT_OBJECT_BSL)

    db_path = IndexBuilder().build(cf, build_calls=True, build_metadata=True, build_fts=True)
    reader = IndexReader(db_path)
    yield reader, cf, db_path
    reader.close()


def _by_type(edges, edge_type):
    return [e for e in edges if e["edge_type"] == edge_type]


def _conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# get_inbound_edges — per edge type
# ---------------------------------------------------------------------------


def test_returns_list_never_none(built):
    reader, _cf, _db = built
    edges = reader.get_inbound_edges("НесуществующийМетодВообще")
    assert isinstance(edges, list)
    assert edges == []


def test_subscription_edge_resolved(built):
    reader, _cf, _db = built
    edges = reader.get_inbound_edges("ПодпискаПриЗаписи")
    subs = _by_type(edges, "subscription")
    assert len(subs) == 1
    e = subs[0]
    assert e["source_name"] == "ПриЗаписиДок"
    assert e["source_kind"] == "EventSubscription"
    assert e["caller_name"] == "ПодпискаПриЗаписи"
    assert e["object_name"] == "ОбработчикиСобытий"
    assert e["resolved"] is True
    assert e["target_key"] is not None


def test_scheduled_job_edge_resolved(built):
    reader, _cf, _db = built
    edges = reader.get_inbound_edges("ВыполнитьОбслуживание")
    jobs = _by_type(edges, "scheduled_job")
    assert len(jobs) == 1
    e = jobs[0]
    assert e["source_name"] == "Обслуживание"
    assert e["source_kind"] == "ScheduledJob"
    assert e["resolved"] is True


def test_cfe_override_edge_resolved(built):
    reader, _cf, db = built
    # Resolve the document object module rel_path for the exact hint.
    with _conn(db) as c:
        row = c.execute(
            "SELECT rel_path FROM modules WHERE object_name=? AND category='Documents'",
            ("ТестовыйДокумент",),
        ).fetchone()
    assert row is not None
    edges = reader.get_inbound_edges("ОбработкаПроведения", module_hint=row["rel_path"])
    cfe = _by_type(edges, "cfe_override")
    assert len(cfe) == 1
    e = cfe[0]
    assert e["source_kind"] == "Extension"
    assert e["object_name"] == "ТестовыйДокумент"
    assert e["caller_name"] == "рсш_ОбработкаПроведения"
    assert e["resolved"] is True


def test_form_event_edge_resolved_with_hint(built):
    reader, _cf, db = built
    with _conn(db) as c:
        row = c.execute("SELECT rel_path FROM modules WHERE is_form=1").fetchone()
    assert row is not None
    edges = reader.get_inbound_edges("ПриСозданииНаСервере", module_hint=row["rel_path"])
    forms = _by_type(edges, "form_event")
    assert len(forms) == 1
    e = forms[0]
    assert e["source_kind"] == "Form"
    assert e["source_name"] == "ФормаДокумента"
    assert e["resolved"] is True


def test_form_event_edge_recall_without_hint(built):
    reader, _cf, _db = built
    # No hint → form method does not resolve to an exact key → recall path.
    edges = reader.get_inbound_edges("ПриСозданииНаСервере")
    forms = _by_type(edges, "form_event")
    assert len(forms) == 1
    assert forms[0]["resolved"] is False


def test_form_recall_skipped_for_nonform_target(built):
    # PERF fix (v1.19.0): a method resolving (exact) to a NON-form module (here a
    # CommonModule) cannot be a form event handler → the form recall scan is skipped,
    # so no spurious form_event edges and no per-node full form_elements scan.
    reader, _cf, _db = built
    edges = reader.get_inbound_edges("ПодпискаПриЗаписи")  # unique CommonModule export
    assert _by_type(edges, "form_event") == []
    # The real (subscription) edge is still found — the skip only drops form scanning.
    assert _by_type(edges, "subscription"), "subscription edge must survive"


# ---------------------------------------------------------------------------
# Recall (Codex №5 + R4 №2)
# ---------------------------------------------------------------------------


def test_subscription_unresolvable_module_recall(built):
    reader, _cf, _db = built
    edges = reader.get_inbound_edges("ОрфанОбработчик")
    subs = _by_type(edges, "subscription")
    assert len(subs) == 1
    assert subs[0]["resolved"] is False
    assert subs[0]["target_key"] is None


def test_subscription_dotless_handler_recall(built):
    reader, _cf, _db = built
    # handler_module="" (dotless) must NOT be dropped (R4 №2 — filter on
    # handler_procedure<>'' not handler_module<>'').
    edges = reader.get_inbound_edges("ОбработчикБезМодуля")
    subs = _by_type(edges, "subscription")
    assert len(subs) == 1
    assert subs[0]["resolved"] is False


def test_cfe_override_extension_only_recall(tmp_path):
    """Extension-only index stores source_path=='' → recall edge (resolved=False)."""
    # Nest under an extra dir so a sibling test that builds at a SHALLOW path
    # (grandparent = shared pytest tmp base) does not pick this extension up via
    # detect_extension_context's 1-level-deep sibling scan.
    ext_dir = os.path.join(tmp_path, "standalone", "ext")
    _write(os.path.join(ext_dir, "Configuration.xml"), _CF_EXT_XML)
    _write(os.path.join(ext_dir, "Documents", "ТестовыйДокумент", "Ext", "ObjectModule.bsl"), _EXT_OBJECT_BSL)
    db_path = IndexBuilder().build(ext_dir, build_calls=True, build_metadata=True, build_fts=False)
    reader = IndexReader(db_path)
    try:
        edges = reader.get_inbound_edges("ОбработкаПроведения")
        cfe = _by_type(edges, "cfe_override")
        assert len(cfe) == 1
        assert cfe[0]["resolved"] is False
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# Cyrillic case-insensitivity (Codex №4)
# ---------------------------------------------------------------------------


def test_cyrillic_casefold_match(built):
    reader, _cf, _db = built
    # Differently-cased query still matches the handler (casefold, not COLLATE NOCASE).
    edges = reader.get_inbound_edges("подпискапризаписи")
    subs = _by_type(edges, "subscription")
    assert len(subs) == 1
    assert subs[0]["caller_name"] == "ПодпискаПриЗаписи"


# ---------------------------------------------------------------------------
# No-op safety on a DB without the tables
# ---------------------------------------------------------------------------


def test_no_op_on_calls_only_index(built):
    reader, _cf, db = built
    # Drop the four source tables → every block degrades to [] (OperationalError-guarded).
    with _conn(db) as c:
        for t in ("extension_overrides", "form_elements", "event_subscriptions", "scheduled_jobs"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        c.commit()
    reader2 = IndexReader(db)
    try:
        assert reader2.get_inbound_edges("ПодпискаПриЗаписи") == []
    finally:
        reader2.close()


# ---------------------------------------------------------------------------
# find_call_hierarchy(include_triggers=...)
# ---------------------------------------------------------------------------


def _indexed_helpers(cf, db_path):
    reader = IndexReader(db_path)
    helpers, resolve_safe = make_helpers(str(cf))
    fmt = detect_format(str(cf))
    bsl = make_bsl_helpers(
        base_path=str(cf),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=fmt,
        idx_reader=reader,
        idx_zero_callers_authoritative=True,
    )
    return bsl, reader


def test_hierarchy_default_has_no_triggers_key(built):
    _reader, cf, db = built
    bsl, reader2 = _indexed_helpers(cf, db)
    try:
        res = bsl["find_call_hierarchy"]("ПодпискаПриЗаписи", depth=1)
        assert res["tree"], "root node expected"
        for node in res["tree"]:
            assert "triggers" not in node
    finally:
        reader2.close()


def test_hierarchy_include_triggers_annotates_node(built):
    _reader, cf, db = built
    bsl, reader2 = _indexed_helpers(cf, db)
    try:
        res = bsl["find_call_hierarchy"]("ПодпискаПриЗаписи", depth=1, include_triggers=True)
        root = next(n for n in res["tree"] if n["name"] == "ПодпискаПриЗаписи")
        assert "triggers" in root
        types = {t["edge_type"] for t in root["triggers"]}
        assert "subscription" in types
    finally:
        reader2.close()
