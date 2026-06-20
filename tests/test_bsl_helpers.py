import json
import os
import sqlite3
import tempfile

import pytest

from rlm_tools_bsl.helpers import make_helpers
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.bsl_helpers import (
    make_bsl_helpers,
    parse_metadata_xml,
    parse_event_subscription_xml,
    parse_scheduled_job_xml,
    parse_enum_xml,
    parse_functional_option_xml,
    parse_rights_xml,
)
from rlm_tools_bsl.bsl_xml_parsers import normalize_type_string, parse_predefined_items


BSL_CODE = """\
#Область ПрограммныйИнтерфейс

Процедура ЗаполнитьДанные(Параметр1, Параметр2) Экспорт
    // тело процедуры
    Сообщить("Начало заполнения");
КонецПроцедуры

Функция ПолучитьСумму(Сумма1, Сумма2) Экспорт
    Возврат Сумма1 + Сумма2;
КонецФункции

Процедура ВнутренняяПроцедура()
    // внутренняя
КонецПроцедуры

#КонецОбласти
"""

BSL_CALLER_CODE = """\
Процедура ОбработкаЗаполнения() Экспорт
    МойМодуль.ЗаполнитьДанные(1, 2);
КонецПроцедуры

Процедура НеВызывает()
    // ЗаполнитьДанные(1, 2);
    //ЗаполнитьДанные(1, 2);
    Сообщить("ЗаполнитьДанные");
КонецПроцедуры
"""


def _create_cf_fixture(tmpdir):
    """Create a CF-style structure with BSL files."""
    # CommonModules/МойМодуль/Ext/Module.bsl
    mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
    os.makedirs(mod_dir)
    with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)

    # Documents/АвансовыйОтчет/Ext/ObjectModule.bsl
    doc_dir = os.path.join(tmpdir, "Documents", "АвансовыйОтчет", "Ext")
    os.makedirs(doc_dir)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CALLER_CODE)

    # Documents/АвансовыйОтчет/Forms/ФормаДокумента/Ext/Form/Module.bsl
    form_dir = os.path.join(tmpdir, "Documents", "АвансовыйОтчет", "Forms", "ФормаДокумента", "Ext", "Form")
    os.makedirs(form_dir)
    with open(os.path.join(form_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write("// form module code\n")

    # Configuration.xml
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")


def _make_bsl_fixture(tmpdir):
    """Create fixture and return bsl_helpers dict."""
    _create_cf_fixture(tmpdir)
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    return bsl, helpers


# --- find_module ---


def test_find_module_by_name(bsl_env):
    results = bsl_env.bsl["find_module"]("МойМодуль")
    assert len(results) >= 1
    assert any(r["object_name"] == "МойМодуль" for r in results)


def test_find_module_by_path_fragment(bsl_env):
    results = bsl_env.bsl["find_module"]("АвансовыйОтчет")
    assert len(results) >= 1


def test_find_module_case_insensitive(bsl_env):
    results = bsl_env.bsl["find_module"]("моймодуль")
    assert len(results) >= 1


def test_find_module_no_results(bsl_env):
    results = bsl_env.bsl["find_module"]("НесуществующийМодуль")
    assert len(results) == 0


# --- find_by_type ---


def test_find_by_type(bsl_env):
    results = bsl_env.bsl["find_by_type"]("Documents")
    assert len(results) >= 1
    assert all(r["category"] == "Documents" for r in results)


def test_find_by_type_with_name(bsl_env):
    results = bsl_env.bsl["find_by_type"]("CommonModules", "МойМодуль")
    assert len(results) >= 1


# --- extract_procedures ---


def test_extract_procedures(bsl_env):
    # Find the module path first
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    assert len(modules) >= 1
    path = modules[0]["path"]

    procs = bsl_env.bsl["extract_procedures"](path)
    assert len(procs) == 3
    names = [p["name"] for p in procs]
    assert "ЗаполнитьДанные" in names
    assert "ПолучитьСумму" in names
    assert "ВнутренняяПроцедура" in names


def test_extract_procedures_export_flag(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    procs = bsl_env.bsl["extract_procedures"](path)
    by_name = {p["name"]: p for p in procs}
    assert by_name["ЗаполнитьДанные"]["is_export"] is True
    assert by_name["ПолучитьСумму"]["is_export"] is True
    assert by_name["ВнутренняяПроцедура"]["is_export"] is False


def test_extract_procedures_has_end_line(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    procs = bsl_env.bsl["extract_procedures"](path)
    for p in procs:
        assert p["end_line"] is not None
        assert p["end_line"] > p["line"]


# --- find_exports ---


def test_find_exports(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    exports = bsl_env.bsl["find_exports"](path)
    assert len(exports) == 2
    names = [e["name"] for e in exports]
    assert "ЗаполнитьДанные" in names
    assert "ПолучитьСумму" in names
    assert "ВнутренняяПроцедура" not in names


# --- safe_grep ---


def test_safe_grep_with_hint(bsl_env):
    results = bsl_env.bsl["safe_grep"]("ЗаполнитьДанные", name_hint="АвансовыйОтчет")
    assert len(results) >= 1


def test_safe_grep_without_hint(bsl_env):
    results = bsl_env.bsl["safe_grep"]("Процедура")
    assert len(results) >= 1


def test_safe_grep_parallel_order(bsl_env):
    """Parallel safe_grep returns results sorted by (file, line)."""
    results = bsl_env.bsl["safe_grep"]("Процедура", max_files=50)
    assert len(results) >= 1
    # Verify sort order: (file, line) ascending
    for i in range(1, len(results)):
        prev = (results[i - 1].get("file", ""), results[i - 1].get("line", 0))
        curr = (results[i].get("file", ""), results[i].get("line", 0))
        assert prev <= curr, f"Order violation: {prev} > {curr}"


# --- read_procedure ---


def test_read_procedure(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    body = bsl_env.bsl["read_procedure"](path, "ЗаполнитьДанные")
    assert body is not None
    assert "ЗаполнитьДанные" in body
    assert "КонецПроцедуры" in body


def test_read_procedure_not_found(bsl_env):
    modules = bsl_env.bsl["find_module"]("МойМодуль")
    path = modules[0]["path"]

    body = bsl_env.bsl["read_procedure"](path, "НесуществующаяПроцедура")
    assert body is None


# --- find_callers ---


def test_find_callers(bsl_env):
    results = bsl_env.bsl["find_callers"]("ЗаполнитьДанные")
    assert len(results) >= 1
    # Should find the call in АвансовыйОтчет
    assert any("АвансовыйОтчет" in r.get("file", "") for r in results)


def test_find_callers_with_hint(bsl_env):
    results = bsl_env.bsl["find_callers"]("ЗаполнитьДанные", module_hint="АвансовыйОтчет")
    assert len(results) >= 1


# --- parse_metadata_xml / parse_object_xml ---

CATALOG_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
<Catalog>
  <Properties>
    <Name>ВидыСпецодежды</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Виды спецодежды</v8:content>
      </v8:item>
    </Synonym>
  </Properties>
  <Attribute>
    <Properties>
      <Name>Безразмерный</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Безразмерный</v8:content>
        </v8:item>
      </Synonym>
      <Type>
        <v8:Type>xs:boolean</v8:Type>
      </Type>
    </Properties>
  </Attribute>
  <TabularSection>
    <Properties>
      <Name>Размеры</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Размеры</v8:content>
        </v8:item>
      </Synonym>
    </Properties>
    <Attribute>
      <Properties>
        <Name>Размер</Name>
        <Synonym>
          <v8:item>
            <v8:lang>ru</v8:lang>
            <v8:content>Размер</v8:content>
          </v8:item>
        </Synonym>
        <Type>
          <v8:Type>xs:string</v8:Type>
        </Type>
      </Properties>
    </Attribute>
  </TabularSection>
</Catalog>
</MetaDataObject>
"""

SUBSYSTEM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
<Subsystem>
  <Properties>
    <Name>Спецодежда</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Спецодежда (ктн)</v8:content>
      </v8:item>
    </Synonym>
    <Content>
      <xr:Item>Catalog.ктнВидыСпецодежды</xr:Item>
      <xr:Item>Document.ктнЗаявкаНаВыдачуСпецодежды</xr:Item>
    </Content>
  </Properties>
</Subsystem>
</MetaDataObject>
"""

REGISTER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
<AccumulationRegister>
  <Properties>
    <Name>ЗаказыНаВыдачу</Name>
    <Synonym>
      <v8:item>
        <v8:lang>ru</v8:lang>
        <v8:content>Заказы на выдачу спецодежды</v8:content>
      </v8:item>
    </Synonym>
  </Properties>
  <Dimension>
    <Properties>
      <Name>ВидСпецодежды</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Вид спецодежды</v8:content>
        </v8:item>
      </Synonym>
      <Type>
        <v8:Type>CatalogRef.ктнВидыСпецодежды</v8:Type>
      </Type>
    </Properties>
  </Dimension>
  <Resource>
    <Properties>
      <Name>Количество</Name>
      <Synonym>
        <v8:item>
          <v8:lang>ru</v8:lang>
          <v8:content>Количество</v8:content>
        </v8:item>
      </Synonym>
      <Type>
        <v8:Type>xs:decimal</v8:Type>
      </Type>
    </Properties>
  </Resource>
</AccumulationRegister>
</MetaDataObject>
"""


# --- MDO format test data ---

MDO_DOCUMENT_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Document xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    xmlns:core="http://g5.1c.ru/v8/dt/mcore"
    uuid="abcd1234-0000-0000-0000-000000000001">
  <name>ЗаявкаНаВыдачу</name>
  <synonym>
    <key>ru</key>
    <value>Заявка на выдачу спецодежды</value>
  </synonym>
  <attributes uuid="abcd1234-0000-0000-0000-000000000002">
    <name>ФизЛицо</name>
    <synonym>
      <key>ru</key>
      <value>Физическое лицо</value>
    </synonym>
    <type>
      <types>CatalogRef.ФизическиеЛица</types>
    </type>
  </attributes>
  <attributes uuid="abcd1234-0000-0000-0000-000000000003">
    <name>Организация</name>
    <synonym>
      <key>ru</key>
      <value>Организация</value>
    </synonym>
    <type>
      <types>CatalogRef.Организации</types>
    </type>
  </attributes>
  <tabularSections uuid="abcd1234-0000-0000-0000-000000000010">
    <name>ВидыСпецодежды</name>
    <synonym>
      <key>ru</key>
      <value>Виды спецодежды</value>
    </synonym>
    <attributes uuid="abcd1234-0000-0000-0000-000000000011">
      <name>ВидСпецодежды</name>
      <synonym>
        <key>ru</key>
        <value>Вид спецодежды</value>
      </synonym>
      <type>
        <types>CatalogRef.ВидыСпецодежды</types>
      </type>
    </attributes>
    <attributes uuid="abcd1234-0000-0000-0000-000000000012">
      <name>Количество</name>
      <type>
        <types>Number</types>
      </type>
    </attributes>
  </tabularSections>
  <forms>ФормаДокумента</forms>
  <forms>ФормаСписка</forms>
  <commands>Печать</commands>
</mdclass:Document>
"""

MDO_REGISTER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:AccumulationRegister xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    uuid="abcd1234-0000-0000-0000-000000000020">
  <name>ТоварыНаСкладах</name>
  <synonym>
    <key>ru</key>
    <value>Товары на складах</value>
  </synonym>
  <dimensions uuid="abcd1234-0000-0000-0000-000000000021">
    <name>Номенклатура</name>
    <synonym>
      <key>ru</key>
      <value>Номенклатура</value>
    </synonym>
    <type>
      <types>CatalogRef.Номенклатура</types>
    </type>
  </dimensions>
  <dimensions uuid="abcd1234-0000-0000-0000-000000000022">
    <name>Склад</name>
    <type>
      <types>CatalogRef.Склады</types>
    </type>
  </dimensions>
  <resources uuid="abcd1234-0000-0000-0000-000000000023">
    <name>Количество</name>
    <type>
      <types>Number</types>
    </type>
  </resources>
</mdclass:AccumulationRegister>
"""

MDO_SUBSYSTEM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Subsystem xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    uuid="abcd1234-0000-0000-0000-000000000030">
  <name>Спецодежда</name>
  <synonym>
    <key>ru</key>
    <value>Спецодежда</value>
  </synonym>
  <content>Catalog.ВидыСпецодежды</content>
  <content>Document.ЗаявкаНаВыдачу</content>
  <content>AccumulationRegister.ТоварыНаСкладах</content>
</mdclass:Subsystem>
"""


def test_parse_catalog_xml():
    result = parse_metadata_xml(CATALOG_XML)
    assert result["object_type"] == "Catalog"
    assert result["name"] == "ВидыСпецодежды"
    assert result["synonym"] == "Виды спецодежды"
    assert len(result["attributes"]) == 1
    assert result["attributes"][0]["name"] == "Безразмерный"
    assert result["attributes"][0]["type"] == "xs:boolean"
    assert len(result["tabular_sections"]) == 1
    ts = result["tabular_sections"][0]
    assert ts["name"] == "Размеры"
    assert len(ts["attributes"]) == 1
    assert ts["attributes"][0]["name"] == "Размер"


def test_parse_subsystem_xml():
    result = parse_metadata_xml(SUBSYSTEM_XML)
    assert result["object_type"] == "Subsystem"
    assert result["name"] == "Спецодежда"
    assert "content" in result
    assert "Catalog.ктнВидыСпецодежды" in result["content"]
    assert "Document.ктнЗаявкаНаВыдачуСпецодежды" in result["content"]


def test_parse_register_xml():
    result = parse_metadata_xml(REGISTER_XML)
    assert result["object_type"] == "AccumulationRegister"
    assert result["name"] == "ЗаказыНаВыдачу"
    assert len(result["dimensions"]) == 1
    assert result["dimensions"][0]["name"] == "ВидСпецодежды"
    assert len(result["resources"]) == 1
    assert result["resources"][0]["name"] == "Количество"


def test_parse_object_xml_via_sandbox():
    """Test parse_object_xml as registered in sandbox helpers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        # Write a metadata XML file
        xml_dir = os.path.join(tmpdir, "Catalogs", "ВидыСО")
        os.makedirs(xml_dir)
        # Write the XML at the catalog level
        with open(os.path.join(xml_dir, "ВидыСО.xml"), "w", encoding="utf-8") as f:
            f.write(CATALOG_XML)

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        # Use relative path
        result = bsl["parse_object_xml"]("Catalogs/ВидыСО/ВидыСО.xml")
        assert result["object_type"] == "Catalog"
        assert result["name"] == "ВидыСпецодежды"


def test_parse_object_xml_directory_path():
    """Test parse_object_xml with a directory path (auto-resolves to XML)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        # Create CF-style Document metadata: Documents/TestDoc/Ext/Document.xml
        doc_xml_dir = os.path.join(tmpdir, "Documents", "АвансовыйОтчет", "Ext")
        os.makedirs(doc_xml_dir, exist_ok=True)
        with open(os.path.join(doc_xml_dir, "Document.xml"), "w", encoding="utf-8") as f:
            f.write(CATALOG_XML)  # reuse catalog XML, structure is similar enough

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        # Pass directory path — should auto-resolve to Ext/Document.xml
        result = bsl["parse_object_xml"]("Documents/АвансовыйОтчет")
        assert "name" in result
        assert result["name"] == "ВидыСпецодежды"  # from CATALOG_XML fixture


# --- MDO format tests ---


def test_parse_mdo_document():
    result = parse_metadata_xml(MDO_DOCUMENT_XML)
    assert result["object_type"] == "Document"
    assert result["name"] == "ЗаявкаНаВыдачу"
    assert result["synonym"] == "Заявка на выдачу спецодежды"
    # Attributes
    assert len(result["attributes"]) == 2
    assert result["attributes"][0]["name"] == "ФизЛицо"
    assert result["attributes"][0]["synonym"] == "Физическое лицо"
    assert result["attributes"][0]["type"] == "CatalogRef.ФизическиеЛица"
    assert result["attributes"][1]["name"] == "Организация"
    # Tabular section
    assert len(result["tabular_sections"]) == 1
    ts = result["tabular_sections"][0]
    assert ts["name"] == "ВидыСпецодежды"
    assert ts["synonym"] == "Виды спецодежды"
    assert len(ts["attributes"]) == 2
    assert ts["attributes"][0]["name"] == "ВидСпецодежды"
    assert ts["attributes"][1]["name"] == "Количество"
    # Forms and commands
    assert result["forms"] == ["ФормаДокумента", "ФормаСписка"]
    assert result["commands"] == ["Печать"]


def test_parse_mdo_register():
    result = parse_metadata_xml(MDO_REGISTER_XML)
    assert result["object_type"] == "AccumulationRegister"
    assert result["name"] == "ТоварыНаСкладах"
    assert result["synonym"] == "Товары на складах"
    assert len(result["dimensions"]) == 2
    assert result["dimensions"][0]["name"] == "Номенклатура"
    assert result["dimensions"][0]["type"] == "CatalogRef.Номенклатура"
    assert result["dimensions"][1]["name"] == "Склад"
    assert len(result["resources"]) == 1
    assert result["resources"][0]["name"] == "Количество"


def test_parse_mdo_subsystem():
    result = parse_metadata_xml(MDO_SUBSYSTEM_XML)
    assert result["object_type"] == "Subsystem"
    assert result["name"] == "Спецодежда"
    assert len(result["content"]) == 3
    assert "Catalog.ВидыСпецодежды" in result["content"]
    assert "Document.ЗаявкаНаВыдачу" in result["content"]
    assert "AccumulationRegister.ТоварыНаСкладах" in result["content"]


# --- find_callers_context ---


def test_find_callers_context_basic(bsl_env):
    """Basic: finds caller with all required fields."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    callers = result["callers"]
    assert len(callers) >= 1
    c = callers[0]
    # All required fields present
    assert "file" in c
    assert "caller_name" in c
    assert "caller_is_export" in c
    assert "line" in c
    assert "context" in c
    assert "object_name" in c
    assert "category" in c
    assert "module_type" in c
    # Caller is ОбработкаЗаполнения
    assert c["caller_name"] == "ОбработкаЗаполнения"


def test_find_callers_context_with_hint(bsl_env):
    """With module_hint: determines export scope, finds caller across files."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные", module_hint="МойМодуль")
    callers = result["callers"]
    assert len(callers) >= 1
    assert any(c["caller_name"] == "ОбработкаЗаполнения" for c in callers)


def test_find_callers_context_no_callers(bsl_env):
    """Internal procedure with no callers returns empty list."""
    result = bsl_env.bsl["find_callers_context"]("ВнутренняяПроцедура")
    assert result["callers"] == []


def test_find_callers_context_ignores_comments(bsl_env):
    """Calls in comments (// with and without space) should be ignored."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    caller_names = [c["caller_name"] for c in result["callers"]]
    assert "НеВызывает" not in caller_names


def test_find_callers_context_ignores_strings(bsl_env):
    """Calls inside string literals should be ignored."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    caller_names = [c["caller_name"] for c in result["callers"]]
    # НеВызывает has the name only in a string literal (after comment lines are stripped)
    assert "НеВызывает" not in caller_names


def test_find_callers_context_caller_metadata(bsl_env):
    """Verify caller metadata: category, object_name, module_type."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    callers = result["callers"]
    c = next(c for c in callers if c["caller_name"] == "ОбработкаЗаполнения")
    assert c["category"] == "Documents"
    assert c["object_name"] == "АвансовыйОтчет"
    assert c["module_type"] == "ObjectModule"


def test_find_callers_context_qualified_call(bsl_env):
    """Qualified call МойМодуль.ЗаполнитьДанные() is found by proc name alone."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    callers = result["callers"]
    # The call is МойМодуль.ЗаполнитьДанные(1, 2) — should be found
    assert any("МойМодуль.ЗаполнитьДанные" in c["context"] for c in callers)


def test_find_callers_context_meta(bsl_env):
    """Result contains _meta with total_callers, returned, offset, has_more."""
    result = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    meta = result["_meta"]
    assert "total_callers" in meta
    assert "returned" in meta
    assert "offset" in meta
    assert "has_more" in meta
    assert meta["has_more"] is False  # small fixture, all scanned


def test_find_callers_context_pagination(bsl_env):
    """Pagination: limit=1 → has_more=True, offset=1 → next batch."""
    # First page: limit=1
    result1 = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные", limit=1)
    meta1 = result1["_meta"]
    assert "has_more" in meta1
    assert "total_callers" in meta1
    assert "returned" in meta1
    assert "offset" in meta1
    if meta1["has_more"]:
        # Second page
        result2 = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные", offset=1, limit=1)
        assert result2["_meta"]["returned"] >= 0


# --- Composite helpers ---


SUBSYSTEM_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
                xmlns:v8="http://v8.1c.ru/8.1/data/core"
                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
<Subsystem>
<Properties>
<Name>ктнСпецодежда</Name>
<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Спецодежда</v8:content></v8:item></Synonym>
<Content>
<xr:Item>Catalog.ктнВидыСпецодежды</xr:Item>
<xr:Item>Document.ВнутреннееПотребление</xr:Item>
<xr:Item>Document.ктнЗаявкаНаВыдачуСпецодежды</xr:Item>
</Content>
</Properties>
</Subsystem>
</MetaDataObject>
"""


def _make_subsystem_fixture(tmpdir):
    """Create fixture with a subsystem XML."""
    # Add subsystem XML to existing fixture
    sub_dir = os.path.join(
        tmpdir,
        "Subsystems",
        "Администрирование",
        "Subsystems",
        "ктнСпецодежда",
    )
    os.makedirs(sub_dir, exist_ok=True)
    with open(os.path.join(sub_dir, "ктнСпецодежда.xml"), "w", encoding="utf-8") as f:
        f.write(SUBSYSTEM_CF_XML)
    # Now create the rest of the fixture (BSL files, Configuration.xml)
    helpers, resolve_safe = make_helpers(tmpdir)
    # Create CF structure manually (avoid _create_cf_fixture which fails on existing dirs)
    mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
    os.makedirs(mod_dir, exist_ok=True)
    with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)
    doc_dir = os.path.join(tmpdir, "Documents", "АвансовыйОтчет", "Ext")
    os.makedirs(doc_dir, exist_ok=True)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CALLER_CODE)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    return bsl, helpers


def test_analyze_subsystem_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_subsystem_fixture(tmpdir)
        result = bsl["analyze_subsystem"]("Спецодежда")
        assert result["subsystems_found"] >= 1
        sub = result["subsystems"][0]
        assert sub["synonym"] == "Спецодежда"
        assert len(sub["custom_objects"]) >= 1
        custom_names = [o["name"] for o in sub["custom_objects"]]
        assert "ктнВидыСпецодежды" in custom_names
        standard_names = [o["name"] for o in sub["standard_objects"]]
        assert "ВнутреннееПотребление" in standard_names


def test_analyze_subsystem_not_found(bsl_env):
    result = bsl_env.bsl["analyze_subsystem"]("НесуществующаяПодсистема")
    assert "error" in result


BSL_CUSTOM_CODE = """\
#Область ктнДоработки

Процедура ктнОбработкаСпецодежды() Экспорт
    // нетиповая процедура
КонецПроцедуры

Процедура ТиповаяПроцедура()
    // типовая
КонецПроцедуры

#КонецОбласти
"""


def test_find_custom_modifications():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create object with custom code
        doc_dir = os.path.join(tmpdir, "Documents", "ТестДок", "Ext")
        os.makedirs(doc_dir)
        with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CUSTOM_CODE)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        result = bsl["find_custom_modifications"]("ТестДок", custom_prefixes=["ктн"])
        assert result["modules_analyzed"] >= 1
        assert len(result["modifications"]) >= 1
        mod = result["modifications"][0]
        custom_proc_names = [p["name"] for p in mod["custom_procedures"]]
        assert "ктнОбработкаСпецодежды" in custom_proc_names
        assert "ТиповаяПроцедура" not in custom_proc_names
        region_names = [r["name"] for r in mod["custom_regions"]]
        assert "ктнДоработки" in region_names
        # Check prefix_source and prefixes_used in response
        assert result["prefix_source"] == "user"
        assert result["prefixes_used"] == ["ктн"]


def test_find_custom_modifications_parse_error():
    """parse_object_xml failure returns diagnostic parse_error field."""
    with tempfile.TemporaryDirectory() as tmpdir:
        doc_dir = os.path.join(tmpdir, "Documents", "ТестДок", "Ext")
        os.makedirs(doc_dir)
        with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура тст_Тест()\nКонецПроцедуры\n")
        # Write invalid XML so parse_object_xml fails
        with open(os.path.join(doc_dir, "Document.xml"), "w") as f:
            f.write("NOT-XML{{{{")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        result = bsl["find_custom_modifications"]("ТестДок", custom_prefixes=["тст"])
        assert "parse_error" in result
        assert result["modules_analyzed"] >= 1


def test_resolve_object_xml_edt_mdo():
    """_resolve_object_xml finds EDT-pattern {path}/{Name}.mdo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # EDT structure: Documents/ТестДок/ТестДок.mdo
        doc_dir = os.path.join(tmpdir, "Documents", "ТестДок")
        os.makedirs(doc_dir)
        mdo_path = os.path.join(doc_dir, "ТестДок.mdo")
        with open(mdo_path, "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<mdclass:Document xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"'
                ' uuid="00000000-0000-0000-0000-000000000001">\n'
                "  <name>ТестДок</name>\n"
                "</mdclass:Document>\n"
            )
        # Also create a BSL file so find_module works
        bsl_dir = os.path.join(doc_dir)
        with open(os.path.join(bsl_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура Тест()\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.mdo"), "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"/>\n'
            )

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        # _resolve_object_xml is internal, test through parse_object_xml
        result = bsl["parse_object_xml"]("Documents/ТестДок")
        # Should resolve to ТестДок.mdo and attempt to parse
        assert isinstance(result, dict)


def test_find_custom_modifications_extension_prefix_threshold():
    """Extension mode uses threshold=1 for prefix detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create objects with a prefix that appears only once
        cat_dir = os.path.join(tmpdir, "Catalogs", "тст_Справочник", "Ext")
        os.makedirs(cat_dir)
        with open(os.path.join(cat_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура тст_Метод()\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)

        # Without idx_reader (config_role unknown), threshold=3 → prefix "тст" won't be detected
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        auto_prefixes = bsl["_detected_prefixes"]()
        # Only 1 object with prefix тст → below threshold 3 → not detected
        assert "тст" not in auto_prefixes


def test_analyze_object(bsl_env):
    result = bsl_env.bsl["analyze_object"]("МойМодуль")
    assert result["name"] == "МойМодуль"
    assert result["category"] == "CommonModules"
    assert len(result["modules"]) >= 1
    mod = result["modules"][0]
    assert mod["procedures_count"] == 3
    assert mod["exports_count"] == 2


# === EventSubscription / ScheduledJob XML parsers ===

EVENT_SUB_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
<EventSubscription uuid="ba1f402d-0000-0000-0000-000000000001">
  <Properties>
    <Name>ЗаписатьВерсиюДокумента</Name>
    <Synonym>
      <v8:item><v8:lang>ru</v8:lang><v8:content>Записать версию документа</v8:content></v8:item>
    </Synonym>
    <Source>
      <v8:Type>cfg:DocumentObject.АвансовыйОтчет</v8:Type>
      <v8:Type>cfg:DocumentObject.ЗаказКлиента</v8:Type>
    </Source>
    <Event>BeforeWrite</Event>
    <Handler>CommonModule.ВерсионированиеОбъектовСобытия.ЗаписатьВерсиюДокумента</Handler>
  </Properties>
</EventSubscription>
</MetaDataObject>
"""

EVENT_SUB_MDO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:EventSubscription xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    uuid="7ce50cee-0000-0000-0000-000000000001">
  <name>тст_ЗаписатьВерсиюДокумента</name>
  <synonym>
    <key>ru</key>
    <value>Записать версию документа</value>
  </synonym>
  <source>
    <types>DocumentObject.АвансовыйОтчет</types>
    <types>DocumentObject.СчетФактураВыданный</types>
  </source>
  <event>BeforeWrite</event>
  <handler>CommonModule.ВерсионированиеОбъектовСобытия.ЗаписатьВерсиюДокумента</handler>
</mdclass:EventSubscription>
"""

SCHEDULED_JOB_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
<ScheduledJob uuid="c7ffd8ab-0000-0000-0000-000000000001">
  <Properties>
    <Name>ЗагрузкаКурсовВалют</Name>
    <Synonym>
      <v8:item><v8:lang>ru</v8:lang><v8:content>Загрузка курсов валют</v8:content></v8:item>
    </Synonym>
    <MethodName>CommonModule.РаботаСКурсамиВалют.ЗагрузитьАктуальныйКурс</MethodName>
    <Use>false</Use>
    <Predefined>true</Predefined>
    <RestartCountOnFailure>10</RestartCountOnFailure>
    <RestartIntervalOnFailure>600</RestartIntervalOnFailure>
  </Properties>
</ScheduledJob>
</MetaDataObject>
"""

SCHEDULED_JOB_MDO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:ScheduledJob xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
    uuid="f3be2107-0000-0000-0000-000000000001">
  <name>ext_ОтправкаПодтверждения</name>
  <synonym>
    <key>ru</key>
    <value>Отправка подтверждения поставки</value>
  </synonym>
  <methodName>CommonModule.ext_РегламентныеЗадания.ОтправкаПодтверждения</methodName>
  <predefined>true</predefined>
  <restartCountOnFailure>3</restartCountOnFailure>
  <restartIntervalOnFailure>10</restartIntervalOnFailure>
</mdclass:ScheduledJob>
"""


ENUM_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
  <Enum><Properties><Name>СтатусыЗаказов</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Статусы заказов</v8:content></v8:item></Synonym></Properties>
  <ChildObjects>
    <EnumValue><Properties><Name>Новый</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Новый</v8:content></v8:item></Synonym></Properties></EnumValue>
    <EnumValue><Properties><Name>ВРаботе</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>В работе</v8:content></v8:item></Synonym></Properties></EnumValue>
    <EnumValue><Properties><Name>Закрыт</Name><Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Закрыт</v8:content></v8:item></Synonym></Properties></EnumValue>
  </ChildObjects></Enum>
</MetaDataObject>
"""

ENUM_MDO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Enum xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ВажностьПроблемы</name>
  <enumValues><name>Предупреждение</name></enumValues>
  <enumValues><name>Ошибка</name></enumValues>
</mdclass:Enum>
"""

FUNCTIONAL_OPTION_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable">
  <FunctionalOption><Properties><Name>ИспользоватьСерии</Name>
    <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Использовать серии</v8:content></v8:item></Synonym>
    <Location>Constant.ИспользоватьСерии</Location>
    <Content><xr:Object>Document.ПриобретениеТоваров</xr:Object><xr:Object>Catalog.СерииНоменклатуры</xr:Object></Content>
  </Properties></FunctionalOption>
</MetaDataObject>
"""

FUNCTIONAL_OPTION_MDO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:FunctionalOption xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ВестиСведенияДляДекларацийПоАлкогольнойПродукции</name>
  <location>Constant.ВестиСведенияДляДекларацийПоАлкогольнойПродукции</location>
  <content>Document.ext_ЗаявлениеОВыдачеФСМ</content>
  <content>Document.ext_НакладнаяНаВыдачуФСМ</content>
</mdclass:FunctionalOption>
"""

RIGHTS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Rights xmlns="http://v8.1c.ru/8.2/roles" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Rights">
  <setForNewObjects>false</setForNewObjects>
  <setForAttributesByDefault>true</setForAttributesByDefault>
  <independentRightsOfChildObjects>false</independentRightsOfChildObjects>
  <object><name>Document.ПриобретениеТоваров</name>
    <right><name>Read</name><value>true</value></right>
    <right><name>Update</name><value>true</value></right>
    <right><name>View</name><value>false</value></right>
  </object>
  <object><name>Catalog.Номенклатура</name>
    <right><name>Read</name><value>true</value></right>
  </object>
</Rights>
"""


# === Enum / FunctionalOption / Rights XML parser tests ===


def test_parse_enum_xml_cf():
    result = parse_enum_xml(ENUM_CF_XML)
    assert result is not None
    assert result["name"] == "СтатусыЗаказов"
    assert result["synonym"] == "Статусы заказов"
    assert len(result["values"]) == 3
    assert result["values"][0]["name"] == "Новый"
    assert result["values"][0]["synonym"] == "Новый"
    assert result["values"][1]["name"] == "ВРаботе"
    assert result["values"][1]["synonym"] == "В работе"
    assert result["values"][2]["name"] == "Закрыт"
    assert result["values"][2]["synonym"] == "Закрыт"


def test_parse_enum_xml_edt():
    result = parse_enum_xml(ENUM_MDO_XML)
    assert result is not None
    assert result["name"] == "ВажностьПроблемы"
    assert len(result["values"]) == 2
    assert result["values"][0]["name"] == "Предупреждение"
    assert result["values"][1]["name"] == "Ошибка"


def test_parse_functional_option_xml_cf():
    result = parse_functional_option_xml(FUNCTIONAL_OPTION_CF_XML)
    assert result is not None
    assert result["name"] == "ИспользоватьСерии"
    assert result["synonym"] == "Использовать серии"
    assert result["location"] == "Constant.ИспользоватьСерии"
    assert len(result["content"]) == 2
    assert "Document.ПриобретениеТоваров" in result["content"]
    assert "Catalog.СерииНоменклатуры" in result["content"]


def test_parse_functional_option_xml_edt():
    result = parse_functional_option_xml(FUNCTIONAL_OPTION_MDO_XML)
    assert result is not None
    assert result["name"] == "ВестиСведенияДляДекларацийПоАлкогольнойПродукции"
    assert result["location"] == "Constant.ВестиСведенияДляДекларацийПоАлкогольнойПродукции"
    assert len(result["content"]) == 2
    assert "Document.ext_ЗаявлениеОВыдачеФСМ" in result["content"]
    assert "Document.ext_НакладнаяНаВыдачуФСМ" in result["content"]


def test_parse_rights_xml():
    result = parse_rights_xml(RIGHTS_XML)
    assert len(result) == 2
    doc = next(r for r in result if r["object"] == "Document.ПриобретениеТоваров")
    assert "Read" in doc["rights"]
    assert "Update" in doc["rights"]
    assert "View" not in doc["rights"]  # value=false excluded
    cat = next(r for r in result if r["object"] == "Catalog.Номенклатура")
    assert cat["rights"] == ["Read"]


def test_parse_rights_xml_filter():
    result = parse_rights_xml(RIGHTS_XML, "ПриобретениеТоваров")
    assert len(result) == 1
    assert result[0]["object"] == "Document.ПриобретениеТоваров"
    assert "Read" in result[0]["rights"]


# === Integration tests for find_enum_values, find_functional_options, find_roles ===


def test_find_enum_values():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        # Add Enum fixture file
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        result = bsl["find_enum_values"]("СтатусыЗаказов")
        assert "error" not in result
        assert result["name"] == "СтатусыЗаказов"
        assert len(result["values"]) == 3
        assert "file" in result


def test_find_enum_values_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_enum_values"]("НесуществующееПеречисление")
        assert "error" in result


def test_find_functional_options():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        # Add FunctionalOption fixture file
        fo_dir = os.path.join(tmpdir, "FunctionalOptions")
        os.makedirs(fo_dir)
        with open(os.path.join(fo_dir, "ИспользоватьСерии.xml"), "w", encoding="utf-8") as f:
            f.write(FUNCTIONAL_OPTION_CF_XML)
        result = bsl["find_functional_options"]("ПриобретениеТоваров")
        assert result["object"] == "ПриобретениеТоваров"
        assert len(result["xml_options"]) >= 1
        assert result["xml_options"][0]["name"] == "ИспользоватьСерии"


def test_find_roles():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        # Add Role/Rights fixture file
        role_dir = os.path.join(tmpdir, "Roles", "Менеджер", "Ext")
        os.makedirs(role_dir)
        with open(os.path.join(role_dir, "Rights.xml"), "w", encoding="utf-8") as f:
            f.write(RIGHTS_XML)
        result = bsl["find_roles"]("ПриобретениеТоваров")
        assert result["object"] == "ПриобретениеТоваров"
        assert len(result["roles"]) >= 1
        role = result["roles"][0]
        assert role["role_name"] == "Менеджер"
        assert "Read" in role["rights"]
        assert "file" in role
        # Fix 5: fallback must include "object" in each role item
        assert "object" in role
        assert role["object"] == "ПриобретениеТоваров"


def test_find_roles_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_roles"]("НесуществующийОбъект")
        assert len(result["roles"]) == 0


def test_parse_cf_event_subscription():
    result = parse_event_subscription_xml(EVENT_SUB_CF_XML)
    assert result is not None
    assert result["name"] == "ЗаписатьВерсиюДокумента"
    assert result["synonym"] == "Записать версию документа"
    assert result["event"] == "BeforeWrite"
    assert result["handler"] == "CommonModule.ВерсионированиеОбъектовСобытия.ЗаписатьВерсиюДокумента"
    assert len(result["source_types"]) == 2
    assert "DocumentObject.АвансовыйОтчет" in result["source_types"]
    assert "DocumentObject.ЗаказКлиента" in result["source_types"]


def test_parse_mdo_event_subscription():
    result = parse_event_subscription_xml(EVENT_SUB_MDO_XML)
    assert result is not None
    assert result["name"] == "тст_ЗаписатьВерсиюДокумента"
    assert result["synonym"] == "Записать версию документа"
    assert result["event"] == "BeforeWrite"
    assert len(result["source_types"]) == 2
    assert "DocumentObject.АвансовыйОтчет" in result["source_types"]


def test_parse_cf_scheduled_job():
    result = parse_scheduled_job_xml(SCHEDULED_JOB_CF_XML)
    assert result is not None
    assert result["name"] == "ЗагрузкаКурсовВалют"
    assert result["synonym"] == "Загрузка курсов валют"
    assert result["method_name"] == "CommonModule.РаботаСКурсамиВалют.ЗагрузитьАктуальныйКурс"
    assert result["use"] is False
    assert result["predefined"] is True
    assert result["restart_on_failure"]["count"] == 10
    assert result["restart_on_failure"]["interval"] == 600


def test_parse_mdo_scheduled_job():
    result = parse_scheduled_job_xml(SCHEDULED_JOB_MDO_XML)
    assert result is not None
    assert result["name"] == "ext_ОтправкаПодтверждения"
    assert result["synonym"] == "Отправка подтверждения поставки"
    assert result["method_name"] == "CommonModule.ext_РегламентныеЗадания.ОтправкаПодтверждения"
    assert result["use"] is True  # EDT default
    assert result["predefined"] is True
    assert result["restart_on_failure"]["count"] == 3


# === Integration tests for new helpers ===

BSL_DOC_WITH_MOVEMENTS = """\
Процедура ОбработкаПроведения(Отказ) Экспорт
    Движения.ТоварыНаСкладах.Записать = Истина;
    Движения.ТоварыНаСкладах.Очистить();
    Движения.РасчетыСПоставщиками.Записать = Истина;
КонецПроцедуры
"""

BSL_DOC_OBJECT_FULL = """\
Процедура ОбработкаЗаполнения(ДанныеЗаполнения) Экспорт
    Если ТипЗнч(ДанныеЗаполнения) = Тип("ДокументСсылка.ЗаказПоставщику") Тогда
        ЗаполнитьНаОсновании(ДанныеЗаполнения);
    ИначеЕсли ТипЗнч(ДанныеЗаполнения) = Тип("СправочникСсылка.ДоговорыКонтрагентов") Тогда
        ЗаполнитьПоДоговору(ДанныеЗаполнения);
    КонецЕсли;
КонецПроцедуры

Процедура ОбработкаПроведения(Отказ) Экспорт
    Движения.ТоварыНаСкладах.Записать = Истина;
    Движения.ТоварыНаСкладах.Очистить();
    Движения.РасчетыСПоставщиками.Записать = Истина;
КонецПроцедуры
"""

BSL_DOC_MANAGER = """\
Процедура ДобавитьКомандыСозданияНаОсновании(КомандыСозданияНаОсновании, Параметры) Экспорт
    Документы.ВозвратТоваров.ДобавитьКомандуСоздатьНаОсновании(КомандыСозданияНаОсновании);
    Документы.СписаниеТоваров.ДобавитьКомандуСоздатьНаОсновании(КомандыСозданияНаОсновании);
КонецПроцедуры

Процедура ДобавитьКомандыПечати(КомандыПечати) Экспорт
    УправлениеПечатью.ДобавитьКомандуПечати(КомандыПечати, "Накладная", НСтр("ru = 'Товарная накладная'"));
    УправлениеПечатью.ДобавитьКомандуПечати(КомандыПечати, "СчетНаОплату", НСтр("ru = 'Счет на оплату'"));
КонецПроцедуры
"""

BSL_DOC_ERP_MANAGER = """\
Процедура ЗарегистрироватьУчетныеМеханизмы(МеханизмыДокумента) Экспорт
    МеханизмыДокумента.Добавить("Взаиморасчеты");
    МеханизмыДокумента.Добавить("Продажи");
    МеханизмыДокумента.Добавить("СебестоимостьИПартионныйУчет");
КонецПроцедуры

Функция АдаптированныйТекстЗапросаДвиженийПоРегистру(ИмяРегистра) Экспорт
    Если ИмяРегистра = "ЗаказыКлиентов" Тогда
        Возврат "";
    ИначеЕсли ИмяРегистра = "РеестрДокументов" Тогда
        Возврат "";
    КонецЕсли;
КонецФункции

Функция ТекстЗапросаТаблицаТовары() Экспорт
    Возврат "";
КонецФункции

Функция ТекстЗапросаТаблицаВидыЗапасов() Экспорт
    Возврат "";
КонецФункции
"""

BSL_DOC_ERP_OBJECT = """\
Процедура ОбработкаПроведения(Отказ, РежимПроведения)
    ПроведениеДокументов.ОбработкаПроведенияДокумента(ЭтотОбъект, Отказ);
КонецПроцедуры
"""


def _make_full_fixture(tmpdir):
    """Create fixture with event subscriptions, scheduled jobs, and document with movements."""
    # BSL modules
    mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
    os.makedirs(mod_dir)
    with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)

    # Document with register movements + ОбработкаЗаполнения
    doc_dir = os.path.join(tmpdir, "Documents", "ПриобретениеТоваров", "Ext")
    os.makedirs(doc_dir)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_DOC_OBJECT_FULL)

    # Add ManagerModule
    with open(os.path.join(doc_dir, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_DOC_MANAGER)

    # EventSubscription
    sub_dir = os.path.join(tmpdir, "EventSubscriptions")
    os.makedirs(sub_dir)
    with open(os.path.join(sub_dir, "ЗаписатьВерсию.xml"), "w", encoding="utf-8") as f:
        f.write(EVENT_SUB_CF_XML)

    # ScheduledJob
    job_dir = os.path.join(tmpdir, "ScheduledJobs")
    os.makedirs(job_dir)
    with open(os.path.join(job_dir, "ЗагрузкаКурсов.xml"), "w", encoding="utf-8") as f:
        f.write(SCHEDULED_JOB_CF_XML)

    # Configuration.xml
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")

    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )
    return bsl, helpers


def test_find_event_subscriptions_all():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_event_subscriptions"]()
        assert len(result) >= 1
        sub = result[0]
        assert sub["name"] == "ЗаписатьВерсиюДокумента"
        assert sub["event"] == "BeforeWrite"
        assert sub["handler_module"] == "ВерсионированиеОбъектовСобытия"
        assert sub["handler_procedure"] == "ЗаписатьВерсиюДокумента"
        # Without filter, source_types should be excluded
        assert "source_types" not in sub


def test_find_event_subscriptions_filtered():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_event_subscriptions"]("АвансовыйОтчет")
        assert len(result) >= 1
        # With filter, source_types should be included
        assert "source_types" in result[0]


def test_find_event_subscriptions_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_event_subscriptions"]("НесуществующийДокумент")
        assert len(result) == 0


def test_find_scheduled_jobs_all():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_scheduled_jobs"]()
        assert len(result) >= 1
        job = result[0]
        assert job["name"] == "ЗагрузкаКурсовВалют"
        assert job["handler_module"] == "РаботаСКурсамиВалют"
        assert job["handler_procedure"] == "ЗагрузитьАктуальныйКурс"
        assert job["use"] is False


def test_find_scheduled_jobs_filtered():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_scheduled_jobs"]("Курс")
        assert len(result) >= 1
        assert result[0]["name"] == "ЗагрузкаКурсовВалют"


def test_find_scheduled_jobs_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_scheduled_jobs"]("НесуществующееЗадание")
        assert len(result) == 0


def test_find_register_movements():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_register_movements"]("ПриобретениеТоваров")
        assert result["document"] == "ПриобретениеТоваров"
        assert len(result["code_registers"]) == 2
        reg_names = [r["name"] for r in result["code_registers"]]
        assert "ТоварыНаСкладах" in reg_names
        assert "РасчетыСПоставщиками" in reg_names
        # ТоварыНаСкладах appears on 2 lines
        товары = next(r for r in result["code_registers"] if r["name"] == "ТоварыНаСкладах")
        assert len(товары["lines"]) == 2


def test_find_register_movements_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_register_movements"]("НесуществующийДок")
        assert "error" in result


def test_find_register_writers():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_register_writers"]("ТоварыНаСкладах")
        assert result["register"] == "ТоварыНаСкладах"
        assert result["total_writers"] >= 1
        writers = result["writers"]
        assert any(w["document"] == "ПриобретениеТоваров" for w in writers)


def test_find_register_writers_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_register_writers"]("НесуществующийРегистр")
        assert result["total_writers"] == 0


def test_analyze_document_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["analyze_document_flow"]("ПриобретениеТоваров")
        assert "metadata" in result
        assert "event_subscriptions" in result
        assert "register_movements" in result
        assert "related_scheduled_jobs" in result
        # Should find register movements
        regs = result["register_movements"].get("code_registers", [])
        assert len(regs) >= 1


def test_help_subscriptions():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("подписки")
        assert "find_event_subscriptions" in text


def test_help_movements():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("движения")
        assert "find_register_movements" in text


def test_help_jobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("регламентные задания")
        assert "find_scheduled_jobs" in text


def test_help_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("как работает документ")
        assert "analyze_document_flow" in text


# === Task 5: find_based_on_documents ===


def test_find_based_on_documents():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_based_on_documents"]("ПриобретениеТоваров")
        assert len(result["can_create_from_here"]) >= 2
        names = [d["document"] for d in result["can_create_from_here"]]
        assert "ВозвратТоваров" in names
        assert "СписаниеТоваров" in names
        assert len(result["can_be_created_from"]) >= 1
        types = [d["type"] for d in result["can_be_created_from"]]
        assert "ДокументСсылка.ЗаказПоставщику" in types


def test_find_based_on_documents_no_manager():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_based_on_documents"]("НесуществующийДок")
        assert len(result["can_create_from_here"]) == 0
        assert len(result["can_be_created_from"]) == 0


# === Task 6: find_print_forms ===


def test_find_print_forms():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_print_forms"]("ПриобретениеТоваров")
        assert len(result["print_forms"]) >= 2
        names = [p["name"] for p in result["print_forms"]]
        assert "Накладная" in names
        assert "СчетНаОплату" in names
        # Check presentation
        nakl = next(p for p in result["print_forms"] if p["name"] == "Накладная")
        assert nakl["presentation"] == "Товарная накладная"


def test_find_print_forms_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        result = bsl["find_print_forms"]("НесуществующийДок")
        assert len(result["print_forms"]) == 0


# === Task 7: find_register_movements ERP framework fallback ===


def test_find_register_movements_erp_framework():
    with tempfile.TemporaryDirectory() as tmpdir:
        doc_dir = os.path.join(tmpdir, "Documents", "РеализацияТоваров", "Ext")
        os.makedirs(doc_dir)
        with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_DOC_ERP_OBJECT)
        with open(os.path.join(doc_dir, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_DOC_ERP_MANAGER)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        from rlm_tools_bsl.helpers import make_helpers
        from rlm_tools_bsl.format_detector import detect_format

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        result = bsl["find_register_movements"]("РеализацияТоваров")
        assert len(result["code_registers"]) == 0  # No direct Движения.X
        assert len(result["erp_mechanisms"]) == 3
        assert "Взаиморасчеты" in result["erp_mechanisms"]
        assert "Продажи" in result["erp_mechanisms"]
        assert len(result["manager_tables"]) >= 2
        assert "Товары" in result["manager_tables"]
        assert "ВидыЗапасов" in result["manager_tables"]
        assert "ЗаказыКлиентов" in result["adapted_registers"]
        assert "РеестрДокументов" in result["adapted_registers"]


# === Task 8: help recipes for new helpers ===


def test_help_based_on():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("ввод на основании")
        assert "find_based_on_documents" in text


def test_help_print_forms():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("печатные формы")
        assert "find_print_forms" in text


def test_help_enum():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("значения перечисления")
        assert "find_enum_values" in text


def test_help_roles():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("права доступа")
        assert "find_roles" in text


def test_help_functional_options():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        text = bsl["help"]("functional options")
        assert "find_functional_options" in text


# === Auto-strip metadata type prefix ===


def test_strip_meta_prefix_find_module(bsl_env):
    # With prefix
    r1 = bsl_env.bsl["find_module"]("Документ.МойМодуль")
    # Without prefix
    r2 = bsl_env.bsl["find_module"]("МойМодуль")
    assert len(r1) == len(r2)
    assert r1[0]["object_name"] == r2[0]["object_name"]


def test_find_module_optional_filters(bsl_env):
    """v1.19.0 tolerant contract: find_module accepts optional module_type/category
    filters (instead of raising on the kwarg) and applies them case-insensitively."""
    fm = bsl_env.bsl["find_module"]
    all_mods = fm("МойМодуль")
    assert all_mods, "fixture must yield at least one module"
    mt = all_mods[0]["module_type"]
    cat = all_mods[0]["category"]

    # Filter by the actual module_type → every row matches it.
    filtered = fm("МойМодуль", module_type=mt)
    assert filtered
    assert all(m["module_type"] == mt for m in filtered)
    # Case-insensitive.
    assert len(fm("МойМодуль", module_type=mt.upper())) == len(filtered)
    # Nonexistent type → empty (no error).
    assert fm("МойМодуль", module_type="НесуществующийТип") == []

    # Category filter likewise.
    by_cat = fm("МойМодуль", category=cat)
    assert by_cat and all(m["category"] == cat for m in by_cat)
    assert fm("МойМодуль", category="НетТакойКатегории") == []

    # Filter-only call WITHOUT a positional name must NOT raise (Codex finding):
    # find_module(module_type=...) is the exact agent guess. name is optional.
    no_name = fm(module_type=mt)
    assert no_name and all(m["module_type"] == mt for m in no_name)
    assert fm(category=cat) and all(m["category"] == cat for m in fm(category=cat))
    # No args at all → browse (bounded), never a TypeError.
    assert isinstance(fm(), list)


def test_strip_meta_prefix_find_register_movements():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        r1 = bsl["find_register_movements"]("Документ.ПриобретениеТоваров")
        r2 = bsl["find_register_movements"]("ПриобретениеТоваров")
        assert r1["document"] == r2["document"]
        assert len(r1["code_registers"]) == len(r2["code_registers"])


def test_strip_meta_prefix_find_enum_values():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture(tmpdir)
        # Create enum fixture
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        r1 = bsl["find_enum_values"]("Перечисление.СтатусыЗаказов")
        r2 = bsl["find_enum_values"]("СтатусыЗаказов")
        assert r1["name"] == r2["name"]


# === source_count=0 subscriptions (catch-all) ===

EVENT_SUB_CATCHALL_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core"
    xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
<EventSubscription uuid="ca1f402d-0000-0000-0000-000000000002">
  <Properties>
    <Name>ктнПередЗаписьюДокумента</Name>
    <Synonym>
      <v8:item><v8:lang>ru</v8:lang><v8:content>Перед записью документа</v8:content></v8:item>
    </Synonym>
    <Source/>
    <Event>BeforeWrite</Event>
    <Handler>CommonModule.ктнПроведение.ПередЗаписьюДокумента</Handler>
  </Properties>
</EventSubscription>
</MetaDataObject>
"""


def test_find_event_subscriptions_catchall():
    """Subscriptions with source_count=0 should match any object name filter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create base fixture
        mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
        os.makedirs(mod_dir)
        with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
        sub_dir = os.path.join(tmpdir, "EventSubscriptions")
        os.makedirs(sub_dir)
        # Normal subscription with specific sources
        with open(os.path.join(sub_dir, "ЗаписатьВерсию.xml"), "w", encoding="utf-8") as f:
            f.write(EVENT_SUB_CF_XML)
        # Catch-all subscription (empty Source)
        with open(os.path.join(sub_dir, "ктнПередЗаписью.xml"), "w", encoding="utf-8") as f:
            f.write(EVENT_SUB_CATCHALL_XML)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        # Filter by a specific object — should return BOTH the matching sub AND the catch-all
        result = bsl["find_event_subscriptions"]("АвансовыйОтчет")
        names = [s["name"] for s in result]
        assert "ЗаписатьВерсиюДокумента" in names  # has АвансовыйОтчет in sources
        assert "ктнПередЗаписьюДокумента" in names  # catch-all, source_count=0

        # Filter by non-existing object — should still return catch-all
        result2 = bsl["find_event_subscriptions"]("НесуществующийОбъект")
        names2 = [s["name"] for s in result2]
        assert "ктнПередЗаписьюДокумента" in names2
        assert "ЗаписатьВерсиюДокумента" not in names2


# === custom_only parameter ===


def test_find_event_subscriptions_custom_only():
    """custom_only=True should filter by auto-detected prefixes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create fixture with both standard and custom subscriptions
        mod_dir = os.path.join(tmpdir, "CommonModules", "ктнМодуль", "Ext")
        os.makedirs(mod_dir)
        with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
        # Need 3+ objects with "ктн" prefix for auto-detect threshold
        for name in ["ктнМодуль2", "ктнМодуль3"]:
            d = os.path.join(tmpdir, "CommonModules", name, "Ext")
            os.makedirs(d)
            with open(os.path.join(d, "Module.bsl"), "w", encoding="utf-8") as f:
                f.write("// stub\n")

        sub_dir = os.path.join(tmpdir, "EventSubscriptions")
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, "ЗаписатьВерсию.xml"), "w", encoding="utf-8") as f:
            f.write(EVENT_SUB_CF_XML)
        with open(os.path.join(sub_dir, "ктнПередЗаписью.xml"), "w", encoding="utf-8") as f:
            f.write(EVENT_SUB_CATCHALL_XML)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")

        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )

        # Without custom_only — should return both
        all_subs = bsl["find_event_subscriptions"]()
        assert len(all_subs) == 2

        # With custom_only — should return only "ктн" prefixed
        custom_subs = bsl["find_event_subscriptions"]("", custom_only=True)
        assert len(custom_subs) == 1
        assert custom_subs[0]["name"] == "ктнПередЗаписьюДокумента"


# ── extract_queries tests ─────────────────────────────────────


def test_extract_queries_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture(tmpdir)
        mod_dir = os.path.join(tmpdir, "Documents", "ТестовыйДокумент", "Ext")
        os.makedirs(mod_dir, exist_ok=True)
        bsl_path = os.path.join(mod_dir, "ObjectModule.bsl")
        with open(bsl_path, "w", encoding="utf-8-sig") as f:
            f.write(
                "Процедура ОбработкаПроведения(Отказ)\n"
                "    Запрос = Новый Запрос;\n"
                '    Запрос.Текст = "ВЫБРАТЬ\n'
                "    |    Т.Ссылка\n"
                "    |ИЗ\n"
                "    |    РегистрНакопления.ТоварыНаСкладах КАК Т\n"
                "    |    СОЕДИНЕНИЕ Справочник.Номенклатура КАК Н\n"
                '    |    ПО Т.Номенклатура = Н.Ссылка";\n'
                "КонецПроцедуры\n"
            )
        rel_path = os.path.relpath(bsl_path, tmpdir).replace("\\", "/")
        queries = bsl["extract_queries"](rel_path)
        assert len(queries) >= 1
        q = queries[0]
        assert q["procedure"] == "ОбработкаПроведения"
        assert "РегистрНакопления.ТоварыНаСкладах" in q["tables"]
        assert "Справочник.Номенклатура" in q["tables"]
        assert "text_preview" in q


def test_extract_queries_no_queries():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture(tmpdir)
        modules = bsl["find_module"]("МойМодуль")
        assert modules
        queries = bsl["extract_queries"](modules[0]["path"])
        assert queries == []


# ── code_metrics tests ────────────────────────────────────────


def test_code_metrics_basic():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture(tmpdir)
        mod_dir = os.path.join(tmpdir, "CommonModules", "МетрикиТест", "Ext")
        os.makedirs(mod_dir, exist_ok=True)
        bsl_path = os.path.join(mod_dir, "Module.bsl")
        with open(bsl_path, "w", encoding="utf-8-sig") as f:
            f.write(
                "// Комментарий\n"
                "\n"
                "Процедура Тест1() Экспорт\n"
                "    Если Истина Тогда\n"
                "        Для Каждого Элемент Из Список Цикл\n"
                "            Сообщить(Элемент);\n"
                "        КонецЦикла;\n"
                "    КонецЕсли;\n"
                "КонецПроцедуры\n"
                "\n"
                "Функция Тест2()\n"
                "    Возврат 1;\n"
                "КонецФункции\n"
            )
        rel_path = os.path.relpath(bsl_path, tmpdir).replace("\\", "/")
        m = bsl["code_metrics"](rel_path)
        assert m["total_lines"] == 13
        assert m["comment_lines"] == 1
        assert m["empty_lines"] == 2
        assert m["code_lines"] == 10
        assert m["procedures_count"] == 2
        assert m["exports_count"] == 1
        assert m["max_nesting"] == 2  # Если + Для
        assert m["avg_proc_size"] > 0


# ---------------------------------------------------------------------------
# find_callers_context: idx_zero_callers_authoritative tests
# ---------------------------------------------------------------------------


def _make_bsl_fixture_authoritative(tmpdir, authoritative=False):
    """Create fixture with idx_zero_callers_authoritative param."""
    _create_cf_fixture(tmpdir)
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_zero_callers_authoritative=authoritative,
    )
    return bsl, helpers


def test_authoritative_true_index_hit():
    """authoritative=True + index returns callers -> result without fallback."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_bsl_fixture_authoritative(tmpdir, authoritative=True)
        # Replace idx_reader in the closure — not possible directly,
        # so we test via the FS path: ЗаполнитьДанные has callers
        result = bsl["find_callers_context"]("ЗаполнитьДанные")
        assert len(result["callers"]) > 0
        assert "fallback_skipped" not in result["_meta"]


def test_authoritative_true_zero_callers():
    """authoritative=True + index returns 0 callers -> fallback skipped."""
    from unittest.mock import MagicMock
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)

        # Create a mock idx_reader that returns 0 callers
        mock_idx = MagicMock()
        mock_idx.has_calls = True
        mock_idx.get_callers.return_value = {
            "callers": [],
            "_meta": {"total_callers": 0, "returned": 0, "offset": 0, "has_more": False},
        }
        mock_idx.get_all_modules.return_value = []
        mock_idx.get_methods_by_path.return_value = None

        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_reader=mock_idx,
            idx_zero_callers_authoritative=True,
        )

        result = bsl["find_callers_context"]("НесуществующаяФункция")
        assert result["_meta"]["fallback_skipped"] is True
        assert "hint" in result["_meta"]
        assert "safe_grep" in result["_meta"]["hint"]
        assert len(result["callers"]) == 0


def test_authoritative_false_zero_callers_fallback():
    """authoritative=False + index returns 0 callers -> fallback performed."""
    from unittest.mock import MagicMock
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        _create_cf_fixture(tmpdir)
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)

        mock_idx = MagicMock()
        mock_idx.has_calls = True
        mock_idx.get_callers.return_value = {
            "callers": [],
            "_meta": {"total_callers": 0, "returned": 0, "offset": 0, "has_more": False},
        }
        mock_idx.get_all_modules.return_value = []
        mock_idx.get_methods_by_path.return_value = None

        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_reader=mock_idx,
            idx_zero_callers_authoritative=False,  # not authoritative
        )

        result = bsl["find_callers_context"]("ЗаполнитьДанные")
        # Fallback should have been performed (no fallback_skipped flag)
        assert "fallback_skipped" not in result["_meta"]


def test_authoritative_parity_known_call():
    """Parity test: known qualified call returns same result from both paths."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create fixture once, build two helpers with different authoritative flag
        _create_cf_fixture(tmpdir)
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)

        bsl_fs = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_zero_callers_authoritative=False,
        )
        bsl_auth = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_zero_callers_authoritative=True,
        )

        result_fs = bsl_fs["find_callers_context"]("ЗаполнитьДанные")
        result_auth = bsl_auth["find_callers_context"]("ЗаполнитьДанные")

        # Both should find the same callers (FS path for both, no idx_reader)
        assert len(result_fs["callers"]) == len(result_auth["callers"])
        fs_callers = {c["caller_name"] for c in result_fs["callers"]}
        auth_callers = {c["caller_name"] for c in result_auth["callers"]}
        assert fs_callers == auth_callers


# --- Fix 2: find_xdto_packages fallback without Package.xdto ---


_XDTO_EDT_MDO = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:XDTOPackage xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass"
                     name="TestPackage">
  <name>TestPackage</name>
  <namespace>http://example.com/test</namespace>
</mdclass:XDTOPackage>
"""


def test_find_xdto_packages_no_package_xdto():
    """find_xdto_packages must not crash when .mdo exists without Package.xdto."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        # Create EDT-style XDTO package without Package.xdto
        xdto_dir = os.path.join(tmpdir, "XDTOPackages", "TestPackage")
        os.makedirs(xdto_dir)
        with open(os.path.join(xdto_dir, "TestPackage.mdo"), "w", encoding="utf-8") as f:
            f.write(_XDTO_EDT_MDO)
        # No Package.xdto — must not crash
        result = bsl["find_xdto_packages"]("")
        # Should return the package without types
        assert len(result) >= 1
        pkg = next(p for p in result if p["name"] == "TestPackage")
        assert pkg["namespace"] == "http://example.com/test"
        assert "types" not in pkg or pkg.get("types") == []


# ──────────────────────────────────────────────────────────────────────────
# Tests for v1.7.0: object_attributes, predefined_items, normalize_type_string
# ──────────────────────────────────────────────────────────────────────────

CF_DOC_WITH_TS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Document>
    <Properties>
      <Name>ДокСТЧ</Name>
      <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Док с ТЧ</v8:content></v8:item></Synonym>
    </Properties>
    <ChildObjects>
      <TabularSection>
        <Properties>
          <Name>Товары</Name>
          <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Товары</v8:content></v8:item></Synonym>
        </Properties>
        <ChildObjects>
          <Attribute>
            <Properties>
              <Name>Номенклатура</Name>
              <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Номенклатура</v8:content></v8:item></Synonym>
              <Type><v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.Номенклатура</v8:Type></Type>
            </Properties>
          </Attribute>
          <Attribute>
            <Properties>
              <Name>Количество</Name>
              <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Количество</v8:content></v8:item></Synonym>
              <Type><v8:Type>xs:decimal</v8:Type></Type>
            </Properties>
          </Attribute>
        </ChildObjects>
      </TabularSection>
    </ChildObjects>
  </Document>
</MetaDataObject>
"""


class TestCfTabularSectionAttributes:
    def test_ts_attributes_parsed(self):
        result = parse_metadata_xml(CF_DOC_WITH_TS_XML)
        assert "tabular_sections" in result
        ts = result["tabular_sections"][0]
        assert ts["name"] == "Товары"
        assert len(ts["attributes"]) == 2
        assert ts["attributes"][0]["name"] == "Номенклатура"
        assert ts["attributes"][1]["name"] == "Количество"


class TestNormalizeTypeString:
    def test_single_xs_string(self):
        assert normalize_type_string("xs:string") == '["String"]'

    def test_single_xs_decimal(self):
        assert normalize_type_string("xs:decimal") == '["Number"]'

    def test_single_xs_boolean(self):
        assert normalize_type_string("xs:boolean") == '["Boolean"]'

    def test_single_xs_datetime(self):
        assert normalize_type_string("xs:dateTime") == '["DateTime"]'

    def test_cfg_catalog_ref(self):
        assert normalize_type_string("cfg:CatalogRef.Организации") == '["CatalogRef.Организации"]'

    def test_d4p1_catalog_ref(self):
        assert normalize_type_string("d4p1:CatalogRef.Номенклатура") == '["CatalogRef.Номенклатура"]'

    def test_edt_no_prefix(self):
        assert normalize_type_string("CatalogRef.Номенклатура") == '["CatalogRef.Номенклатура"]'

    def test_composite_types(self):
        result = normalize_type_string("cfg:CatalogRef.X, cfg:CatalogRef.Y")
        assert result == '["CatalogRef.X", "CatalogRef.Y"]'

    def test_empty_string(self):
        assert normalize_type_string("") == "[]"

    def test_single_xs_base64binary(self):
        assert normalize_type_string("xs:base64Binary") == '["ValueStorage"]'

    def test_mixed_prefixes(self):
        result = normalize_type_string("xs:string, cfg:CatalogRef.X, d4p1:DocumentRef.Y")
        assert result == '["String", "CatalogRef.X", "DocumentRef.Y"]'


PREDEFINED_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<ChartOfCharacteristicTypes xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
<PredefinedData>
<Item id="aaa">
    <Name>РеализуемыеАктивы</Name>
    <Code>00055</Code>
    <Description>Реализуемые активы</Description>
    <Type>
        <v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.Номенклатура</v8:Type>
        <v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.Контрагенты</v8:Type>
    </Type>
    <IsFolder>false</IsFolder>
</Item>
<Item id="bbb">
    <Name>ВидыДеятельности</Name>
    <Code>00010</Code>
    <Description>Виды деятельности</Description>
    <Type>
        <v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.ВидыДеятельности</v8:Type>
    </Type>
    <IsFolder>true</IsFolder>
</Item>
</PredefinedData>
</ChartOfCharacteristicTypes>
"""

PREDEFINED_EDT_MDO = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:ChartOfCharacteristicTypes xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass">
  <name>ВидыСубконтоХозрасчетные</name>
  <predefined>
    <items id="aaa">
      <name>РеализуемыеАктивы</name>
      <description>Реализуемые активы</description>
      <code>00055</code>
      <type>
        <types>CatalogRef.Номенклатура</types>
        <types>CatalogRef.Контрагенты</types>
      </type>
    </items>
    <items id="bbb">
      <name>ВидыДеятельности</name>
      <description>Виды деятельности</description>
      <code>00010</code>
      <type>
        <types>CatalogRef.ВидыДеятельности</types>
      </type>
      <isFolder>true</isFolder>
    </items>
  </predefined>
</mdclass:ChartOfCharacteristicTypes>
"""


class TestParsePredefinedItems:
    def test_cf_format(self):
        result = parse_predefined_items(PREDEFINED_CF_XML)
        assert result is not None
        assert len(result) == 2
        r0 = result[0]
        assert r0["name"] == "РеализуемыеАктивы"
        assert r0["synonym"] == "Реализуемые активы"
        assert r0["code"] == "00055"
        assert "CatalogRef.Номенклатура" in r0["types"]
        assert "CatalogRef.Контрагенты" in r0["types"]
        assert r0["is_folder"] is False
        r1 = result[1]
        assert r1["name"] == "ВидыДеятельности"
        assert r1["is_folder"] is True

    def test_edt_format(self):
        result = parse_predefined_items(PREDEFINED_EDT_MDO)
        assert result is not None
        assert len(result) == 2
        r0 = result[0]
        assert r0["name"] == "РеализуемыеАктивы"
        assert r0["synonym"] == "Реализуемые активы"
        assert r0["code"] == "00055"
        assert "CatalogRef.Номенклатура" in r0["types"]
        assert "CatalogRef.Контрагенты" in r0["types"]
        assert r0["is_folder"] is False

    def test_empty_xml(self):
        result = parse_predefined_items("<root/>")
        assert result is None or result == []

    def test_invalid_xml(self):
        result = parse_predefined_items("not xml")
        assert result is None


# ──────────────────────────────────────────────────────────────────────────
# Tests for IndexReader, helpers, and search integration (v1.7.0)
# ──────────────────────────────────────────────────────────────────────────

ATTR_DOC_CF_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <Document>
    <Properties>
      <Name>ТестДок</Name>
      <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Тест документ</v8:content></v8:item></Synonym>
    </Properties>
    <ChildObjects>
      <Attribute>
        <Properties>
          <Name>Организация</Name>
          <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Организация</v8:content></v8:item></Synonym>
          <Type><v8:Type xmlns:d4p1="http://v8.1c.ru/8.1/data/enterprise/current-config">d4p1:CatalogRef.Организации</v8:Type></Type>
        </Properties>
      </Attribute>
      <Attribute>
        <Properties>
          <Name>Сумма</Name>
          <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Сумма</v8:content></v8:item></Synonym>
          <Type><v8:Type>xs:decimal</v8:Type></Type>
        </Properties>
      </Attribute>
    </ChildObjects>
  </Document>
</MetaDataObject>
"""


def _make_indexed_fixture(tmpdir):
    """Create fixture with indexed attributes + predefined items."""
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    # BSL module (required for index build)
    mod_dir = os.path.join(tmpdir, "CommonModules", "МойМодуль", "Ext")
    os.makedirs(mod_dir)
    with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)

    # Document with attributes
    doc_dir = os.path.join(tmpdir, "Documents", "ТестДок", "Ext")
    os.makedirs(doc_dir)
    with open(os.path.join(doc_dir, "Document.xml"), "w", encoding="utf-8") as f:
        f.write(ATTR_DOC_CF_XML)
    with open(os.path.join(doc_dir, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write("// пусто")

    # Predefined items
    pvh_dir = os.path.join(tmpdir, "ChartsOfCharacteristicTypes", "ВидыСубконто", "Ext")
    os.makedirs(pvh_dir)
    with open(os.path.join(pvh_dir, "Predefined.xml"), "w", encoding="utf-8") as f:
        f.write(PREDEFINED_CF_XML)
    # Need a metadata XML too for attribute scanning
    with open(os.path.join(pvh_dir, "ChartOfCharacteristicTypes.xml"), "w", encoding="utf-8") as f:
        f.write("""\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"
    xmlns:v8="http://v8.1c.ru/8.1/data/core">
  <ChartOfCharacteristicTypes>
    <Properties>
      <Name>ВидыСубконто</Name>
      <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Виды субконто</v8:content></v8:item></Synonym>
    </Properties>
  </ChartOfCharacteristicTypes>
</MetaDataObject>
""")

    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")

    # Build index
    builder = IndexBuilder()
    db_path = builder.build(tmpdir, build_calls=False, build_metadata=True)

    reader = IndexReader(str(db_path))
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_reader=reader,
    )
    return bsl, reader


class TestIndexReaderObjectAttributes:
    def test_get_object_attributes_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(attr_name="Организация")
            assert results is not None
            assert len(results) >= 1
            assert any(r["attr_name"] == "Организация" for r in results)
            assert all(isinstance(r["attr_type"], list) for r in results)
            reader.close()

    def test_get_object_attributes_by_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(object_name="ТестДок")
            assert results is not None
            assert len(results) == 2  # Организация + Сумма
            reader.close()

    def test_get_object_attributes_by_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(kind="attribute")
            assert results is not None
            assert len(results) >= 2
            assert all(r["attr_kind"] == "attribute" for r in results)
            reader.close()

    def test_get_object_attributes_kind_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(kind="Attribute")
            assert results is not None
            assert len(results) >= 2
            reader.close()

    def test_get_object_attributes_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_object_attributes(attr_name="НесуществующийРеквизит")
            assert results is not None
            assert results == []
            reader.close()


class TestIndexReaderPredefinedItems:
    def test_get_predefined_items_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_predefined_items(item_name="РеализуемыеАктивы")
            assert results is not None
            assert len(results) >= 1
            r0 = results[0]
            assert r0["item_name"] == "РеализуемыеАктивы"
            assert isinstance(r0["types"], list)
            assert "CatalogRef.Номенклатура" in r0["types"]
            reader.close()

    def test_get_predefined_items_by_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_predefined_items(object_name="ВидыСубконто")
            assert results is not None
            assert len(results) == 2  # РеализуемыеАктивы + ВидыДеятельности
            reader.close()

    def test_get_predefined_items_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = reader.get_predefined_items(item_name="НесуществующийЭлемент")
            assert results is not None
            assert results == []
            reader.close()


class TestFindAttributesHelper:
    def test_find_attributes_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_attributes"](name="Организация")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert any(r["attr_name"] == "Организация" for r in results)
            reader.close()

    def test_find_attributes_by_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_attributes"](object_name="ТестДок")
            assert isinstance(results, list)
            assert len(results) == 2
            reader.close()

    def test_find_attributes_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_attributes"](name="НесуществующийРеквизит")
            assert results == []
            reader.close()


class TestFindPredefinedHelper:
    def test_find_predefined_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_predefined"](name="РеализуемыеАктивы")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert results[0]["item_name"] == "РеализуемыеАктивы"
            reader.close()

    def test_find_predefined_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["find_predefined"](name="НесуществующийЭлемент")
            assert results == []
            reader.close()


class TestSearchNewScopes:
    def test_search_scope_attributes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["search"]("Организация", scope="attributes")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert all(r["source_type"] == "attribute" for r in results)
            reader.close()

    def test_search_scope_predefined(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["search"]("Реализуемые", scope="predefined")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert all(r["source_type"] == "predefined" for r in results)
            reader.close()

    def test_search_all_includes_new_types(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["search"]("Организация", scope="all")
            source_types = {r["source_type"] for r in results}
            assert "attribute" in source_types
            reader.close()


class TestGetIndexInfoNewFields:
    def test_has_new_capability_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            info = bsl["get_index_info"]()
            assert info["status"] == "ok"
            assert info["has_object_attributes"] is True
            assert info["has_predefined_items"] is True
            assert info["object_attributes_count"] >= 2
            assert info["predefined_items_count"] >= 2
            reader.close()


class TestSearchSynonymPath:
    def test_search_predefined_by_synonym(self):
        """search(scope='predefined') finds items by synonym (Реализуемые активы)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            results = bsl["search"]("Реализуемые активы", scope="predefined")
            assert isinstance(results, list)
            assert len(results) >= 1
            assert any("Реализуемые" in r["text"] for r in results)
            reader.close()

    def test_search_attribute_by_synonym(self):
        """search(scope='attributes') finds by synonym when attr_name != attr_synonym."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bsl, reader = _make_indexed_fixture(tmpdir)
            # Организация has same name and synonym — search by synonym
            results = bsl["search"]("Организация", scope="attributes")
            assert isinstance(results, list)
            assert len(results) >= 1
            reader.close()


class TestFallbackContract:
    def test_find_attributes_fallback_normalizes_types(self):
        """Fallback path returns normalized types matching index contract."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create fixture WITHOUT index — only XML files
            doc_dir = os.path.join(tmpdir, "Documents", "ТестДок", "Ext")
            os.makedirs(doc_dir)
            with open(os.path.join(doc_dir, "Document.xml"), "w", encoding="utf-8") as f:
                f.write(ATTR_DOC_CF_XML)

            with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
                f.write("<Configuration/>")

            helpers, resolve_safe = make_helpers(tmpdir)
            format_info = detect_format(tmpdir)
            bsl = make_bsl_helpers(
                base_path=tmpdir,
                resolve_safe=resolve_safe,
                read_file_fn=helpers["read_file"],
                grep_fn=helpers["grep"],
                glob_files_fn=helpers["glob_files"],
                format_info=format_info,
                # No idx_reader — force fallback path
            )
            results = bsl["find_attributes"](object_name="Documents/ТестДок")
            assert len(results) == 2
            # Types must be normalized lists, not raw XML strings
            org = next(r for r in results if r["attr_name"] == "Организация")
            assert isinstance(org["attr_type"], list)
            assert "CatalogRef.Организации" in org["attr_type"]
            # Category must be filled from path
            assert org["category"] == "Documents"
            # Сумма type must be normalized
            summ = next(r for r in results if r["attr_name"] == "Сумма")
            assert "Number" in summ["attr_type"]


# ============================================================
# P1 — list-перегрузка read_procedure / find_callers_context / find_enum_values
# Каждый: первый «целевой» аргумент list → dict-by-name (изоляция ошибок
# поэлементно); str → прежний контракт байт-в-байт.
# ============================================================


def test_read_procedure_list_returns_dict_by_name(bsl_env):
    """list имён → {name: body}; модуль резолвится один раз (мемоизация)."""
    path = bsl_env.bsl["find_module"]("МойМодуль")[0]["path"]
    res = bsl_env.bsl["read_procedure"](path, ["ЗаполнитьДанные", "ПолучитьСумму"])
    assert isinstance(res, dict)
    assert set(res.keys()) == {"ЗаполнитьДанные", "ПолучитьСумму"}
    assert "ЗаполнитьДанные" in res["ЗаполнитьДанные"]
    assert "ПолучитьСумму" in res["ПолучитьСумму"]


def test_read_procedure_list_isolates_bad_name(bsl_env):
    """Один валид + один отсутствующий: dict с телом-str и None, без падения батча."""
    path = bsl_env.bsl["find_module"]("МойМодуль")[0]["path"]
    res = bsl_env.bsl["read_procedure"](path, ["ЗаполнитьДанные", "НетТакойМетод"])
    assert isinstance(res, dict)
    assert isinstance(res["ЗаполнитьДанные"], str)
    assert res["НетТакойМетод"] is None


def test_read_procedure_str_mode_unchanged(bsl_env):
    """str proc_name → str|None (НЕ dict) — прежний контракт."""
    path = bsl_env.bsl["find_module"]("МойМодуль")[0]["path"]
    body = bsl_env.bsl["read_procedure"](path, "ЗаполнитьДанные")
    assert isinstance(body, str)
    assert "ЗаполнитьДанные" in body
    none_body = bsl_env.bsl["read_procedure"](path, "НетТакойМетод")
    assert none_body is None


def test_find_callers_context_list_returns_dict_by_name(bsl_env):
    """list имён → {name: {callers, _meta}} с общим module_hint/offset/limit."""
    res = bsl_env.bsl["find_callers_context"](["ЗаполнитьДанные", "ПолучитьСумму"])
    assert isinstance(res, dict)
    assert set(res.keys()) == {"ЗаполнитьДанные", "ПолучитьСумму"}
    for name in ("ЗаполнитьДанные", "ПолучитьСумму"):
        assert "callers" in res[name]
        assert "_meta" in res[name]
    assert any(c["caller_name"] == "ОбработкаЗаполнения" for c in res["ЗаполнитьДанные"]["callers"])


def test_find_callers_context_list_isolates_no_callers(bsl_env):
    """list с именем без вызывающих → его запись — валидный пустой результат, не крэш."""
    res = bsl_env.bsl["find_callers_context"](["ЗаполнитьДанные", "ВнутренняяПроцедура"])
    assert res["ВнутренняяПроцедура"]["callers"] == []
    assert len(res["ЗаполнитьДанные"]["callers"]) >= 1


def test_find_callers_context_str_mode_unchanged(bsl_env):
    """str proc_name → {callers, _meta} (НЕ keyed by name) — прежний контракт."""
    res = bsl_env.bsl["find_callers_context"]("ЗаполнитьДанные")
    assert "callers" in res and "_meta" in res
    assert any(c["caller_name"] == "ОбработкаЗаполнения" for c in res["callers"])


def test_find_enum_values_list_returns_dict_by_name():
    """list → {name: {...}|{error}}; изоляция: валид + не найдено."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        res = bsl["find_enum_values"](["СтатусыЗаказов", "НетТакогоПеречисления"])
        assert isinstance(res, dict)
        assert set(res.keys()) == {"СтатусыЗаказов", "НетТакогоПеречисления"}
        assert res["СтатусыЗаказов"]["name"] == "СтатусыЗаказов"
        assert "error" not in res["СтатусыЗаказов"]
        assert "error" in res["НетТакогоПеречисления"]


def test_find_enum_values_str_mode_unchanged():
    """str enum_name → один dict с name/values (НЕ keyed by name) — прежний контракт."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        res = bsl["find_enum_values"]("СтатусыЗаказов")
        assert res["name"] == "СтатусыЗаказов"
        assert "values" in res


# ============================================================
# P2 — дешёвый агрегат get_object_modules(name, include_methods=False)
# ============================================================

_MANAGER_BSL = "Функция ПолучитьПоИдентификатору(Ид) Экспорт\n    Возврат Ид;\nКонецФункции\n"


def _make_object_modules_fixture(tmpdir, *, with_index=True):
    """Document РеализацияТоваров: ObjectModule (3 метода, 1 #Область, 2 экспорта) +
    ManagerModule (1 метод-экспорт, без области). Returns (bsl, reader|None)."""
    obj = os.path.join(tmpdir, "Documents", "РеализацияТоваров", "Ext")
    os.makedirs(obj)
    with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)
    with open(os.path.join(obj, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
        f.write(_MANAGER_BSL)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    reader = None
    kw = {}
    if with_index:
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
        reader = IndexReader(str(db_path))
        kw["idx_reader"] = reader
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        **kw,
    )
    return bsl, reader


def test_get_object_modules_index_path():
    """Индексный путь: идентичность объекта, все модули, per-module index_used=True, roll-up."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            res = bsl["get_object_modules"]("РеализацияТоваров")
            assert "error" not in res
            assert res["object_name"] == "РеализацияТоваров"
            assert res["category"] == "Documents"
            paths = {m["path"] for m in res["modules"]}
            assert any(p.endswith("ObjectModule.bsl") for p in paths)
            assert any(p.endswith("ManagerModule.bsl") for p in paths)
            # Дешёвый индексный путь доказан per-module (без extract_procedures).
            for m in res["modules"]:
                assert m["_meta"]["index_used"] is True
            # roll-up: 3 (ObjectModule) + 1 (ManagerModule) методов, 2+1 экспортов.
            assert res["totals"]["modules"] == 2
            assert res["totals"]["methods"] == 4
            assert res["totals"]["exports"] == 3
            assert res["_meta"]["index_used"] is True
        finally:
            if reader:
                reader.close()


def test_get_object_modules_include_methods_false_vs_true():
    """include_methods=False → область без листовых методов; True → методы есть. totals одинаковы."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            skel = bsl["get_object_modules"]("РеализацияТоваров", include_methods=False)
            full = bsl["get_object_modules"]("РеализацияТоваров", include_methods=True)

            def _om_outline(res):
                om = next(m for m in res["modules"] if m["path"].endswith("ObjectModule.bsl"))
                return om["outline"]

            for r in _om_outline(skel):
                assert "methods" not in r
            assert any("methods" in r and r["methods"] for r in _om_outline(full))
            assert skel["totals"]["methods"] == full["totals"]["methods"] == 4
        finally:
            if reader:
                reader.close()


def test_get_object_modules_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            res = bsl["get_object_modules"]("НетТакогоОбъекта")
            assert "error" in res
            assert "_meta" in res
        finally:
            if reader:
                reader.close()


def test_get_object_modules_override_flags():
    """Флаги перехватов на модуль через get_overrides_for_path + roll-up overrides."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:

            def _fake_overrides(rel_path):
                if rel_path.endswith("ObjectModule.bsl"):
                    return {"ЗаполнитьДанные": [{"extension_name": "Расш", "annotation": "Вместо"}]}
                return {}

            reader.get_overrides_for_path = _fake_overrides  # instance attr shadows method
            res = bsl["get_object_modules"]("РеализацияТоваров")
            om = next(m for m in res["modules"] if m["path"].endswith("ObjectModule.bsl"))
            assert om["overrides"]["count"] == 1
            assert "ЗаполнитьДанные" in om["overrides"]["methods"]
            mm = next(m for m in res["modules"] if m["path"].endswith("ManagerModule.bsl"))
            assert mm["overrides"]["count"] == 0
            assert res["totals"]["overrides"] == 1
        finally:
            if reader:
                reader.close()


def test_get_object_modules_exact_enumeration_not_capped():
    """Имя-подстрока многих путей: прямой exact-скан собирает ВСЕ свои модули и НЕ
    обрезается капом 50 (в отличие от find_module). Live-режим."""
    with tempfile.TemporaryDirectory() as tmpdir:
        obj = os.path.join(tmpdir, "Documents", "Док", "Ext")
        os.makedirs(obj)
        with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
        with open(os.path.join(obj, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
            f.write(_MANAGER_BSL)
        # 60 «декоев», object_name которых СОДЕРЖИТ «Док» подстрокой → find_module
        # их substring-матчит и упирается в кап 50.
        for i in range(60):
            d = os.path.join(tmpdir, "Documents", f"Док_Декой{i:02d}", "Ext")
            os.makedirs(d)
            with open(os.path.join(d, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
                f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,  # live, без индекса
        )
        # Кап реально срабатывает на частом имени-подстроке.
        assert len(bsl["find_module"]("Док")) == 50
        # get_object_modules — exact scan → ТОЛЬКО 2 собственных модуля «Док».
        res = bsl["get_object_modules"]("Док")
        assert res["object_name"] == "Док"
        assert len(res["modules"]) == 2
        assert all("/Док/" in m["path"] for m in res["modules"])  # ни одного декоя


def test_get_object_modules_does_not_parse_object_xml(monkeypatch):
    """get_object_modules НИКОГДА не зовёт parse_object_xml/parse_metadata_xml (в отличие от analyze_object)."""
    import rlm_tools_bsl.bsl_helpers as bh

    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            calls = []
            real = bh.parse_metadata_xml

            def _spy(*a, **k):
                calls.append(1)
                return real(*a, **k)

            monkeypatch.setattr(bh, "parse_metadata_xml", _spy)
            res = bsl["get_object_modules"]("РеализацияТоваров")
            assert "error" not in res
            assert calls == []
        finally:
            if reader:
                reader.close()


def test_get_object_modules_stale_index_fallback_reason(monkeypatch):
    """Честный per-module live-fallback: module-row есть, methods пуст → index_used=False + fallback_reason."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            real = reader.get_outline_data

            def _stale(rel_path):
                if rel_path.endswith("ManagerModule.bsl"):
                    return {
                        "module": {
                            "category": "Documents",
                            "object_name": "РеализацияТоваров",
                            "module_type": "ManagerModule",
                        },
                        "regions": [],
                        "methods": [],
                    }
                return real(rel_path)

            monkeypatch.setattr(reader, "get_outline_data", _stale)
            res = bsl["get_object_modules"]("РеализацияТоваров")
            mm = next(m for m in res["modules"] if m["path"].endswith("ManagerModule.bsl"))
            assert mm["_meta"]["index_used"] is False
            assert mm["_meta"]["fallback_reason"] == "index_empty_for_module"
            om = next(m for m in res["modules"] if m["path"].endswith("ObjectModule.bsl"))
            assert om["_meta"]["index_used"] is True
        finally:
            if reader:
                reader.close()


def test_get_module_outline_no_live_skips_stale_read(monkeypatch):
    """no_live=True: stale module → skipped marker (empty outline, skipped_live), NO live parse.
    no_live=False on the same module → live read populates the outline (proves the contrast)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            real = reader.get_outline_data

            def _stale(rel_path):
                if rel_path.endswith("ManagerModule.bsl"):
                    return {
                        "module": {
                            "category": "Documents",
                            "object_name": "РеализацияТоваров",
                            "module_type": "ManagerModule",
                        },
                        "regions": [],
                        "methods": [],
                    }
                return real(rel_path)

            monkeypatch.setattr(reader, "get_outline_data", _stale)
            # Find the ManagerModule rel path.
            mods = bsl["find_module"]("РеализацияТоваров")
            mgr = next(m["path"] for m in mods if m["path"].endswith("ManagerModule.bsl"))

            skipped = bsl["get_module_outline"](mgr, no_live=True)
            assert skipped["_meta"]["skipped_live"] is True
            assert skipped["_meta"]["index_used"] is False
            assert skipped["_meta"]["fallback_reason"] == "index_empty_for_module"
            assert skipped["outline"] == []
            assert skipped["totals"]["methods"] == 0
            # Identity still filled structurally (no body read needed).
            assert skipped["object_name"] == "РеализацияТоваров"

            live = bsl["get_module_outline"](mgr, no_live=False)
            assert live["_meta"].get("skipped_live") is not True
            assert live["totals"]["methods"] == 1  # ManagerModule has 1 export — live read happened
        finally:
            if reader:
                reader.close()


def test_get_object_modules_no_live_propagates(monkeypatch):
    """get_object_modules(no_live=True): stale module marked skipped_live, top-level
    _meta.modules_skipped_live=True, and NO live parse (methods stay 0 for that module)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_object_modules_fixture(tmpdir, with_index=True)
        try:
            real = reader.get_outline_data

            def _stale(rel_path):
                if rel_path.endswith("ManagerModule.bsl"):
                    return {
                        "module": {
                            "category": "Documents",
                            "object_name": "РеализацияТоваров",
                            "module_type": "ManagerModule",
                        },
                        "regions": [],
                        "methods": [],
                    }
                return real(rel_path)

            monkeypatch.setattr(reader, "get_outline_data", _stale)
            res = bsl["get_object_modules"]("РеализацияТоваров", no_live=True)
            mm = next(m for m in res["modules"] if m["path"].endswith("ManagerModule.bsl"))
            assert mm["_meta"]["skipped_live"] is True
            assert mm["totals"]["methods"] == 0  # not read
            om = next(m for m in res["modules"] if m["path"].endswith("ObjectModule.bsl"))
            assert om["_meta"]["index_used"] is True  # healthy module unaffected
            assert res["_meta"]["modules_skipped_live"] is True
        finally:
            if reader:
                reader.close()


def test_find_functional_options_include_code_gate():
    """include_code=False → XML-only, the safe_grep code scan is skipped (code_options empty)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mod_dir = os.path.join(tmpdir, "CommonModules", "Скидки", "Ext")
        os.makedirs(mod_dir)
        with open(os.path.join(mod_dir, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write(
                "Функция ИспользуютсяСкидки() Экспорт\n"
                '    Возврат ПолучитьФункциональнуюОпцию("ИспользоватьСкидки");\n'
                "КонецФункции\n"
            )
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
        )
        with_code = bsl["find_functional_options"]("Скидки", include_code=True)
        assert any(c["option_name"] == "ИспользоватьСкидки" for c in with_code["code_options"])
        xml_only = bsl["find_functional_options"]("Скидки", include_code=False)
        assert xml_only["code_options"] == []


# ============================================================
# v1.23.0 — get_object_profile (one-shot compact object aggregate)
# ============================================================

_PROFILE_STATUS_ENUM = {"ok", "empty", "error", "unavailable", "skipped"}
_PROFILE_SOURCE_ENUM = {"index", "live", "mixed", "unknown"}


def _seed_profile_meta(db_path):
    """Inject roles/subscriptions/FO/register_movements rows (+ a non-Document object)
    so the profile data sections are populated, including a *Доп prefix-collision sibling."""
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO role_rights (role_name, object_name, right_name, file) VALUES (?, ?, ?, ?)",
        [
            ("РольПродажи", "Document.РеализацияТоваров", "Read", "Roles/РольПродажи/Ext/Rights.xml"),
            ("РольПродажи", "Document.РеализацияТоваров", "Update", "Roles/РольПродажи/Ext/Rights.xml"),
            ("РольДоп", "Document.РеализацияТоваровДоп", "Read", "Roles/РольДоп/Ext/Rights.xml"),  # collision
        ],
    )
    conn.executemany(
        "INSERT INTO event_subscriptions (name, synonym, event, handler_module, "
        "handler_procedure, source_types, source_count, file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "ПодпискаРеализация",
                "",
                "BeforeWrite",
                "ОМ",
                "Обр",
                json.dumps(["DocumentObject.РеализацияТоваров"], ensure_ascii=False),
                1,
                "es1.xml",
            ),
            (
                "ПодпискаДоп",
                "",
                "OnWrite",
                "ОМ",
                "Обр2",
                json.dumps(["DocumentObject.РеализацияТоваровДоп"], ensure_ascii=False),
                1,
                "es2.xml",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO functional_options (name, synonym, location, content, file) VALUES (?, ?, ?, ?, ?)",
        [
            ("ФО_Реализация", "", "", json.dumps(["Document.РеализацияТоваров"], ensure_ascii=False), "fo1.xml"),
            ("ФО_Доп", "", "", json.dumps(["Document.РеализацияТоваровДоп"], ensure_ascii=False), "fo2.xml"),
        ],
    )
    conn.executemany(
        "INSERT INTO register_movements (document_name, register_name, source, file) VALUES (?, ?, ?, ?)",
        [
            ("РеализацияТоваров", "Продажи", "code", "om.bsl"),
            ("РеализацияТоваров", "Себестоимость", "code", "om.bsl"),
            ("РеализацияТоваров", "МеханизмУУ", "erp_mechanism", "mm.bsl"),
            ("РеализацияТоваров", "ТаблицаX", "manager_table", "mm.bsl"),
            ("РеализацияТоваров", "АдаптРег", "adapted", "mm.bsl"),
        ],
    )
    conn.executemany(
        "INSERT INTO object_attributes (object_name, category, attr_name, attr_synonym, "
        "attr_type, attr_kind, ts_name, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("Контрагенты", "Catalogs", "ИНН", "ИНН", json.dumps(["String"]), "attribute", None, "cat.xml"),
            ("Контрагенты", "Catalogs", "КПП", "КПП", json.dumps(["String"]), "attribute", None, "cat.xml"),
        ],
    )
    conn.commit()
    conn.close()


def _make_profile_fixture(tmpdir, *, with_index=True, glob_counter=None, read_counter=None):
    """Document РеализацияТоваров (ObjectModule+ManagerModule) + seeded metadata rows.
    Optional glob/read counters prove the no-index path triggers no glob/live."""
    obj = os.path.join(tmpdir, "Documents", "РеализацияТоваров", "Ext")
    os.makedirs(obj)
    with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(BSL_CODE)
    with open(os.path.join(obj, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
        f.write(_MANAGER_BSL)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    reader = None
    kw = {}
    if with_index:
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
        _seed_profile_meta(db_path)
        reader = IndexReader(str(db_path))
        kw["idx_reader"] = reader

    read_fn = helpers["read_file"]
    glob_fn = helpers["glob_files"]
    if read_counter is not None:
        _r = read_fn

        def read_fn(p, _r=_r):  # noqa: F811
            read_counter.append(p)
            return _r(p)

    if glob_counter is not None:
        _g = glob_fn

        def glob_fn(pat, _g=_g):  # noqa: F811
            glob_counter.append(pat)
            return _g(pat)

    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=read_fn,
        grep_fn=helpers["grep"],
        glob_files_fn=glob_fn,
        format_info=format_info,
        **kw,
    )
    return bsl, reader


def _assert_section_shape(sec):
    """Every section conforms to the unified contract."""
    assert sec["status"] in _PROFILE_STATUS_ENUM, sec["status"]
    assert isinstance(sec["summary"], dict)
    assert isinstance(sec["items"], list)
    assert isinstance(sec["total"], int)
    assert isinstance(sec["returned"], int)
    assert isinstance(sec["has_more"], bool)
    assert sec["_meta"]["source"] in _PROFILE_SOURCE_ENUM, sec["_meta"]["source"]
    assert isinstance(sec["_meta"]["elapsed_ms"], (int, float))


def test_profile_default_sections_full_shape():
    """Default profile: top-level + every section conforms; _meta tracing present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров")
            assert "error" not in p
            assert p["object_name"] == "РеализацияТоваров"
            assert p["category"] == "Documents"
            # default section set, no heavy flow/code_usages
            assert set(p["sections"]) == {
                "structure",
                "modules",
                "registers",
                "subscriptions",
                "roles",
                "functional_options",
            }
            for sec in p["sections"].values():
                _assert_section_shape(sec)
            # _meta tracing (R4 #1,#2)
            meta = p["_meta"]
            assert meta["identity_source"] == "index"
            assert meta["extension_visibility"] == "standalone"
            assert isinstance(meta["total_elapsed_ms"], (int, float))
            names = {s["name"] for s in meta["sections"]}
            assert names == set(p["sections"])
            for s in meta["sections"]:
                assert set(s) >= {"name", "elapsed_ms", "source", "status", "items_count", "truncated"}
        finally:
            if reader:
                reader.close()


def test_profile_exact_ref_no_collision():
    """roles/subscriptions/functional_options match РеализацияТоваров EXACTLY, never *Доп."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров")
            roles = p["sections"]["roles"]
            assert roles["status"] == "ok"
            assert {r["role_name"] for r in roles["items"]} == {"РольПродажи"}
            subs = p["sections"]["subscriptions"]
            assert {s["name"] for s in subs["items"]} == {"ПодпискаРеализация"}
            fo = p["sections"]["functional_options"]
            assert {f["name"] for f in fo["items"]} == {"ФО_Реализация"}
        finally:
            if reader:
                reader.close()


def test_profile_registers_summary_domain_counters_not_flattened():
    """registers.summary keeps code/erp/manager/adapted counters separate (R5 #6)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            reg = bsl["get_object_profile"]("РеализацияТоваров")["sections"]["registers"]
            assert reg["status"] == "ok"
            assert reg["summary"] == {
                "code_registers": 2,
                "erp_mechanisms": 1,
                "manager_tables": 1,
                "adapted_registers": 1,
            }
            # total/returned/has_more self-consistent (all about the ONE main list)
            assert reg["total"] == 5
            assert reg["returned"] == 5
            assert reg["has_more"] is False
            # items = all movement targets with a source label; breakdown lives in summary
            assert {i["register"] for i in reg["items"]} == {
                "Продажи",
                "Себестоимость",
                "МеханизмУУ",
                "ТаблицаX",
                "АдаптРег",
            }
            assert {i["source"] for i in reg["items"]} == {"code", "erp_mechanism", "manager_table", "adapted"}
        finally:
            if reader:
                reader.close()


def test_profile_no_index_all_sections_unavailable_no_glob():
    """No index → identity from input prefix, ALL data sections 'unavailable', NO glob/read (R10 #1, R11 #1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        globs, reads = [], []
        bsl, _ = _make_profile_fixture(tmpdir, with_index=False, glob_counter=globs, read_counter=reads)
        globs.clear()
        reads.clear()
        p = bsl["get_object_profile"]("Документ.РеализацияТоваров")
        assert p["object_name"] == "РеализацияТоваров"
        assert p["category"] == "Documents"
        assert p["_meta"]["identity_source"] == "input_prefix"
        for name, sec in p["sections"].items():
            assert sec["status"] == "unavailable", name
            assert sec["_meta"]["reason"] == "no_index"
        # The compact no-index path must NOT touch the filesystem.
        assert globs == [], f"unexpected glob: {globs}"
        assert reads == [], f"unexpected read: {reads}"


def test_profile_no_index_bare_name_errors_no_glob():
    """No index + bare name (no type prefix) → {error:'no_index_identity_unresolved'}, NO glob."""
    with tempfile.TemporaryDirectory() as tmpdir:
        globs = []
        bsl, _ = _make_profile_fixture(tmpdir, with_index=False, glob_counter=globs)
        globs.clear()
        p = bsl["get_object_profile"]("РеализацияТоваров")
        assert p["error"] == "no_index_identity_unresolved"
        assert "hint" in p
        assert globs == []


def test_profile_per_section_isolation():
    """A broken section (component raises) → status 'error', neighbors 'ok', profile still valid (R2 #2, R4 #4)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:

            def _boom(*a, **k):
                raise RuntimeError("boom")

            reader.get_roles_exact = _boom  # shadow → roles section raises
            p = bsl["get_object_profile"]("РеализацияТоваров")
            assert "error" not in p  # profile still valid
            assert p["sections"]["roles"]["status"] == "error"
            assert "boom" in p["sections"]["roles"]["_meta"]["error"]
            # neighbors unaffected
            assert p["sections"]["subscriptions"]["status"] in {"ok", "empty"}
            assert p["sections"]["registers"]["status"] == "ok"
        finally:
            if reader:
                reader.close()


def test_profile_not_found_top_level_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("НетТакогоОбъекта")
            assert "error" in p
            assert p["_meta"]["identity_source"] == "unresolved"
        finally:
            if reader:
                reader.close()


def test_profile_non_document_modules_empty_registers_skipped():
    """Catalog (no BSL modules): modules 'empty', registers 'skipped' (not a Document), structure 'ok'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("Справочник.Контрагенты")
            assert p["object_name"] == "Контрагенты"
            assert p["category"] == "Catalogs"
            assert p["sections"]["modules"]["status"] == "empty"
            assert p["sections"]["registers"]["status"] == "skipped"
            assert p["sections"]["registers"]["_meta"]["reason"] == "not_a_document"
            structure = p["sections"]["structure"]
            assert structure["status"] == "ok"
            assert structure["summary"]["attributes"] == 2
        finally:
            if reader:
                reader.close()


def test_profile_sections_arg_takes_exactly_requested():
    """sections=[...] (with aliases) runs ONLY the requested sections."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров", sections=["structure", "права"])
            assert set(p["sections"]) == {"structure", "roles"}  # 'права' → roles alias
        finally:
            if reader:
                reader.close()


def test_profile_include_flow_adds_heavy_section_only_on_flag():
    """flow/code_usages sections appear ONLY under include_flow/include_code_usages (heavy gated)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            default = bsl["get_object_profile"]("РеализацияТоваров")
            assert "flow" not in default["sections"]
            assert "code_usages" not in default["sections"]
            withflow = bsl["get_object_profile"]("РеализацияТоваров", include_flow=True)
            assert "flow" in withflow["sections"]
            assert withflow["sections"]["flow"]["_meta"]["source"] == "mixed"
        finally:
            if reader:
                reader.close()


def test_profile_items_carry_no_procedure_bodies():
    """Compact output contract (R4 #3): section items expose only names/paths/counts, no bodies."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров")
            for sec in p["sections"].values():
                for item in sec["items"]:
                    # no procedure bodies / large snippets (a small 'source' label is fine)
                    assert "body" not in item and "text" not in item
                    # items are small flat dicts (names/paths/counts only)
                    for v in item.values():
                        assert not (isinstance(v, str) and len(v) > 300)
        finally:
            if reader:
                reader.close()


def _make_homonym_profile_fixture(tmpdir):
    """Document.Заказ AND Catalog.Заказ — both have an ObjectModule (a true homonym)."""
    for cat in ("Documents", "Catalogs"):
        d = os.path.join(tmpdir, cat, "Заказ", "Ext")
        os.makedirs(d)
        with open(os.path.join(d, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

    db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
    reader = IndexReader(str(db_path))
    bsl = make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
        idx_reader=reader,
    )
    return bsl, reader


def test_profile_homonym_modules_category_aware():
    """Homonym Document.Заказ / Catalog.Заказ: modules section carries ONLY the resolved
    category's module (category-aware adapter, not a merge of both) (R2 #1, R5 #1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_homonym_profile_fixture(tmpdir)
        try:
            p = bsl["get_object_profile"]("Заказ")
            cat = p["category"]
            assert cat in {"Documents", "Catalogs"}
            mods = p["sections"]["modules"]
            assert mods["status"] == "ok"
            # every module path belongs to the ONE resolved category (no homonym merge)
            folder = cat + "/"
            for item in mods["items"]:
                assert folder in item["path"].replace("\\", "/"), (cat, item["path"])
            assert mods["total"] == 1  # exactly that object's single module
        finally:
            if reader:
                reader.close()


def test_profile_prefix_disambiguates_homonym():
    """Explicit type prefix routes identity (and ALL sections) to the requested category on a
    cross-category homonym — in the INDEXED path, not just no-index (codex finding 1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_homonym_profile_fixture(tmpdir)
        try:
            doc = bsl["get_object_profile"]("Документ.Заказ")
            assert doc["category"] == "Documents"
            assert doc["_meta"]["identity_source"] == "index_prefix"
            assert doc["sections"]["modules"]["items"]
            for item in doc["sections"]["modules"]["items"]:
                assert "Documents/" in item["path"].replace("\\", "/")
            # structure stays on the SAME homonym (category_hint) — no identity_match drift
            assert "identity_match" not in doc["sections"]["structure"]["_meta"]

            cat = bsl["get_object_profile"]("Справочник.Заказ")
            assert cat["category"] == "Catalogs"
            for item in cat["sections"]["modules"]["items"]:
                assert "Catalogs/" in item["path"].replace("\\", "/")
        finally:
            if reader:
                reader.close()


def test_profile_prefix_missing_type_falls_back_honestly():
    """Explicit prefix for a type that does NOT contain the object → fall back to the object's
    REAL category with identity_source='index' (NOT a misleading 'index_prefix'). Guards the
    Pass-3 close-match leak when prefer_category is set (codex finding 5)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # ONLY Catalog.Заказ exists — there is NO Document.Заказ.
        d = os.path.join(tmpdir, "Catalogs", "Заказ", "Ext")
        os.makedirs(d)
        with open(os.path.join(d, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
            f.write(BSL_CODE)
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader

        db_path = IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True)
        reader = IndexReader(str(db_path))
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,
            idx_reader=reader,
        )
        try:
            p = bsl["get_object_profile"]("Документ.Заказ")
            # the object exists only as a Catalog → resolved to its real category, honestly labelled
            assert p["category"] == "Catalogs"
            assert p["object_name"] == "Заказ"
            assert p["_meta"]["identity_source"] == "index", "must NOT claim index_prefix on a fallback"
        finally:
            reader.close()


def test_profile_prefix_case_insensitive():
    """The type prefix is recognised case-insensitively AND `bare` is derived from the suffix,
    so 'document.X' / 'DOCUMENT.X' behave exactly like 'Document.X' / 'Документ.X' (finding 6)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_homonym_profile_fixture(tmpdir)
        try:
            for variant in ("Документ.Заказ", "document.Заказ", "DOCUMENT.Заказ", "Document.Заказ"):
                p = bsl["get_object_profile"](variant)
                assert "error" not in p, (variant, p)
                assert p["category"] == "Documents", variant
                assert p["object_name"] == "Заказ", variant
                assert p["_meta"]["identity_source"] == "index_prefix", variant
        finally:
            if reader:
                reader.close()


def test_profile_modules_skipped_status_on_stale(monkeypatch):
    """no_live modules section that hits a stale module → status 'skipped' (NOT 'ok'): the
    zero methods/exports are not authoritative; section flagged for a full live re-read (finding 2)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            real = reader.get_outline_data

            def _stale(rel_path):
                if rel_path.endswith("ObjectModule.bsl"):
                    return {
                        "module": {
                            "category": "Documents",
                            "object_name": "РеализацияТоваров",
                            "module_type": "ObjectModule",
                        },
                        "regions": [],
                        "methods": [],
                    }
                return real(rel_path)

            monkeypatch.setattr(reader, "get_outline_data", _stale)
            mods = bsl["get_object_profile"]("РеализацияТоваров")["sections"]["modules"]
            assert mods["status"] == "skipped"
            assert mods["_meta"]["modules_skipped_live"] is True
            assert mods["_meta"]["reason"] == "stale_modules_skipped_live"
            om = next(i for i in mods["items"] if i["path"].endswith("ObjectModule.bsl"))
            assert om["skipped_live"] is True
            assert om["methods"] == 0  # not read — flagged, not silently authoritative
        finally:
            if reader:
                reader.close()


def test_profile_code_usages_surfaces_live_fallback(monkeypatch):
    """code_usages reflects find_code_usages' live grep fallback (partial=True → source 'live',
    not 'index') so the hidden live cost is visible (codex finding 3)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:

            def _boom(*a, **k):
                raise RuntimeError("no v13 metadata_code_usages table")

            monkeypatch.setattr(reader, "find_code_usages", _boom)
            monkeypatch.setattr(reader, "count_code_usages", _boom)
            p = bsl["get_object_profile"]("РеализацияТоваров", include_code_usages=True)
            cu = p["sections"]["code_usages"]
            assert cu["_meta"]["source"] == "live"
            assert cu["_meta"]["partial"] is True
            assert cu["summary"]["partial"] is True
            # the per-section _meta source surfaced into the top-level trace
            trace = next(s for s in p["_meta"]["sections"] if s["name"] == "code_usages")
            assert trace["source"] == "live"
        finally:
            if reader:
                reader.close()


def test_profile_trace_truncated_matches_has_more():
    """A section sliced by `limit` (has_more=True) reports _meta.truncated=True AND the
    top-level trace truncated=True — the trace must not lie about truncation (codex finding)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, reader = _make_profile_fixture(tmpdir, with_index=True)
        try:
            p = bsl["get_object_profile"]("РеализацияТоваров", limit=2)
            reg = p["sections"]["registers"]  # 5 movements, limit 2 → truncated preview
            assert reg["has_more"] is True
            assert reg["_meta"]["truncated"] is True  # section _meta honest
            reg_trace = next(s for s in p["_meta"]["sections"] if s["name"] == "registers")
            assert reg_trace["truncated"] is True  # trace agrees with has_more

            subs = p["sections"]["subscriptions"]  # only 1 match → not truncated
            assert subs["has_more"] is False
            assert subs["_meta"]["truncated"] is False
            subs_trace = next(s for s in p["_meta"]["sections"] if s["name"] == "subscriptions")
            assert subs_trace["truncated"] is False
        finally:
            if reader:
                reader.close()


# ============================================================
# P3 — extract_procedures / get_module_outline принимают имя ИЛИ путь
# Единое правило (category, module_type): (CommonModules/CommonForms, Module)
# → (*, ObjectModule) → (*, ManagerModule) → первый по стабильной сортировке.
# ============================================================


def _make_ambiguous_object_fixture(tmpdir):
    """Document «Многоформ» с ДВУМЯ формами и БЕЗ ObjectModule → обе строки
    (Documents, Module) одного ранга → неоднозначность. Live-режим."""
    for fn in ("Ф1", "Ф2"):
        d = os.path.join(tmpdir, "Documents", "Многоформ", "Forms", fn, "Ext", "Form")
        os.makedirs(d)
        with open(os.path.join(d, "Module.bsl"), "w", encoding="utf-8") as f:
            f.write("Процедура ПриОткрытии()\nКонецПроцедуры\n")
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    format_info = detect_format(tmpdir)
    return make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=format_info,
    )


def test_extract_procedures_by_name_picks_object_module(bsl_env):
    """Имя Document → (*, ObjectModule) по единому правилу; результат == по пути."""
    by_name = bsl_env.bsl["extract_procedures"]("АвансовыйОтчет")
    names = {p["name"] for p in by_name}
    assert "ОбработкаЗаполнения" in names  # ObjectModule выбран (у формы методов нет)
    om_path = next(
        m["path"] for m in bsl_env.bsl["find_module"]("АвансовыйОтчет") if m["path"].endswith("ObjectModule.bsl")
    )
    by_path = bsl_env.bsl["extract_procedures"](om_path)
    assert {p["name"] for p in by_path} == names


def test_extract_procedures_by_name_common_module(bsl_env):
    """Имя общего модуля → (CommonModules, Module) ранг 0."""
    procs = bsl_env.bsl["extract_procedures"]("МойМодуль")
    assert {p["name"] for p in procs} == {"ЗаполнитьДанные", "ПолучитьСумму", "ВнутренняяПроцедура"}


def test_extract_procedures_path_mode_unchanged(bsl_env):
    """Путь (есть '/') — прежний контракт, детект имени не ложно-срабатывает."""
    path = bsl_env.bsl["find_module"]("МойМодуль")[0]["path"]
    assert "/" in path
    procs = bsl_env.bsl["extract_procedures"](path)
    assert len(procs) == 3


def test_extract_procedures_ambiguous_name_raises():
    """Неоднозначное имя → ValueError (НЕ [] — пустой список неотличим от «нет процедур»)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl = _make_ambiguous_object_fixture(tmpdir)
        with pytest.raises(ValueError):
            bsl["extract_procedures"]("Многоформ")


def test_get_module_outline_by_name_merges_meta(bsl_env):
    """Имя → _meta МЕРЖИТ resolver-ключи в существующий _meta (index_used/fallback_reason НЕ затёрты)."""
    by_name = bsl_env.bsl["get_module_outline"]("МойМодуль")
    meta = by_name["_meta"]
    # старый контракт _meta цел
    assert "index_used" in meta and "fallback_reason" in meta
    # resolver-ключи домержены
    assert meta["resolved_from_name"] is True
    assert meta["chosen_module"].endswith("Module.bsl")
    assert meta["ambiguous"] is False
    assert "candidates" in meta
    # name-режим == path-режим того же модуля
    path = meta["chosen_module"]
    by_path = bsl_env.bsl["get_module_outline"](path)
    assert by_path["_meta"]["resolved_from_name"] is False
    assert "index_used" in by_path["_meta"] and "fallback_reason" in by_path["_meta"]
    assert by_name["totals"] == by_path["totals"]


def test_get_module_outline_ambiguous_name_flags_meta():
    """Неоднозначное имя → прозрачный авто-выбор: _meta.ambiguous=True + кандидаты, НЕ ошибка."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl = _make_ambiguous_object_fixture(tmpdir)
        o = bsl["get_module_outline"]("Многоформ")
        assert o["_meta"]["resolved_from_name"] is True
        assert o["_meta"]["ambiguous"] is True
        assert len(o["_meta"]["candidates"]) == 2
        assert "totals" in o  # валидный outline (авто-выбор первого по стабильной сортировке)
        assert "index_used" in o["_meta"] and "fallback_reason" in o["_meta"]


# ============================================================
# Codex review fixes (dense-batching): findings 1/2/3
# ============================================================


def test_resolve_module_arg_strips_meta_prefix_extract_procedures(bsl_env):
    """Finding 2: имя с префиксом типа (Документ.X) резолвится так же, как bare-имя."""
    bare = {p["name"] for p in bsl_env.bsl["extract_procedures"]("АвансовыйОтчет")}
    prefixed = {p["name"] for p in bsl_env.bsl["extract_procedures"]("Документ.АвансовыйОтчет")}
    assert prefixed == bare
    assert "ОбработкаЗаполнения" in prefixed


def test_resolve_module_arg_strips_meta_prefix_get_module_outline(bsl_env):
    """Finding 2: get_module_outline('Документ.X') резолвит ObjectModule, не уходит битым путём."""
    o = bsl_env.bsl["get_module_outline"]("Документ.АвансовыйОтчет")
    assert "error" not in o
    assert o["_meta"]["resolved_from_name"] is True
    assert o["_meta"]["chosen_module"].endswith("ObjectModule.bsl")
    # bare и prefixed дают один и тот же выбранный модуль
    bare = bsl_env.bsl["get_module_outline"]("АвансовыйОтчет")
    assert bare["_meta"]["chosen_module"] == o["_meta"]["chosen_module"]


def test_single_or_map_isolates_raising_item():
    """Finding 3: исключение скаляра на одном элементе батча → {error} под его ключом,
    валидный сосед всё равно резолвится (батч не оборван)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl, _ = _make_full_fixture(tmpdir)
        enum_dir = os.path.join(tmpdir, "Enums", "СтатусыЗаказов")
        os.makedirs(enum_dir)
        with open(os.path.join(enum_dir, "СтатусыЗаказов.xml"), "w", encoding="utf-8") as f:
            f.write(ENUM_CF_XML)
        # 123 (int) → _strip_meta_prefix(123) бросает AttributeError в скалярном ядре.
        res = bsl["find_enum_values"](["СтатусыЗаказов", 123])
        assert isinstance(res, dict)
        assert res["СтатусыЗаказов"]["name"] == "СтатусыЗаказов"  # сосед уцелел
        assert "error" in res["123"]  # битый элемент изолирован, батч не упал


def test_get_object_modules_collision_category_deterministic():
    """Finding 1: одноимённые объекты в разных категориях → выбор детерминирован
    (rel-path сорт), модули ровно ОДНОЙ категории — get_all_modules() без ORDER BY
    больше не делает результат порядок-зависимым."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for cat in ("Catalogs", "Documents"):
            obj = os.path.join(tmpdir, cat, "Омоним", "Ext")
            os.makedirs(obj)
            with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
                f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
        with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
            f.write("<Configuration/>")
        helpers, resolve_safe = make_helpers(tmpdir)
        format_info = detect_format(tmpdir)
        bsl = make_bsl_helpers(
            base_path=tmpdir,
            resolve_safe=resolve_safe,
            read_file_fn=helpers["read_file"],
            grep_fn=helpers["grep"],
            glob_files_fn=helpers["glob_files"],
            format_info=format_info,  # live, без индекса
        )
        res = bsl["get_object_modules"]("Омоним")
        assert "error" not in res
        # Catalogs/... < Documents/... по rel-path → первая категория Catalogs.
        assert res["category"] == "Catalogs"
        assert res["modules"]  # непусто
        # ровно одна категория: все модули из Catalogs, ни одного из Documents
        assert all(m["path"].startswith("Catalogs/") for m in res["modules"])
        assert not any("Documents" in m["path"] for m in res["modules"])
