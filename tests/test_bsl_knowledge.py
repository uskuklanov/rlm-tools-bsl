import re

from rlm_tools_bsl.bsl_knowledge import (
    BSL_PATTERNS,
    EFFORT_LEVELS,
    EffortConfig,
    RLM_START_DESCRIPTION,
    _BUSINESS_RECIPES,
    _RECIPE_ALIASES,
    _match_recipe,
    get_strategy,
)


# --- BSL_PATTERNS ---


def test_all_patterns_compile():
    """All regex patterns must compile without error."""
    for name, pattern in BSL_PATTERNS.items():
        compiled = re.compile(pattern)
        assert compiled is not None, f"Pattern {name} failed to compile"


def test_procedure_def_pattern():
    pattern = re.compile(BSL_PATTERNS["procedure_def"])
    assert pattern.search("Процедура МояПроцедура(Параметр1) Экспорт")
    assert pattern.search("Функция МояФункция()")
    assert not pattern.search("// комментарий")


def test_procedure_end_pattern():
    pattern = re.compile(BSL_PATTERNS["procedure_end"])
    assert pattern.search("КонецПроцедуры")
    assert pattern.search("  КонецФункции")
    assert not pattern.search("Процедура")


def test_module_call_pattern():
    pattern = re.compile(BSL_PATTERNS["module_call"])
    m = pattern.search("ОбщийМодуль.МояФункция(Параметры)")
    assert m is not None
    assert m.group(1) == "ОбщийМодуль"
    assert m.group(2) == "МояФункция"


def test_region_patterns():
    start = re.compile(BSL_PATTERNS["region_start"])
    end = re.compile(BSL_PATTERNS["region_end"])
    m = start.search("#Область ПрограммныйИнтерфейс")
    assert m is not None
    assert m.group(1) == "ПрограммныйИнтерфейс"
    assert end.search("#КонецОбласти")


# --- EFFORT_LEVELS ---


def test_effort_levels_keys():
    assert set(EFFORT_LEVELS.keys()) == {"low", "medium", "high", "max"}


def test_effort_levels_types():
    for name, config in EFFORT_LEVELS.items():
        assert isinstance(config, EffortConfig)
        assert config.max_execute_calls > 0
        assert config.max_llm_calls > 0
        assert config.safe_grep_max_files > 0
        assert len(config.guidance) > 0


def test_effort_levels_ordering():
    """Higher effort levels should have higher limits."""
    levels = ["low", "medium", "high", "max"]
    for i in range(len(levels) - 1):
        a = EFFORT_LEVELS[levels[i]]
        b = EFFORT_LEVELS[levels[i + 1]]
        assert b.max_execute_calls >= a.max_execute_calls


# --- get_strategy ---


def test_strategy_contains_critical_warning():
    text = get_strategy("medium", None)
    assert "CRITICAL" in text
    assert "23,000" in text or "23000" in text or "timeout" in text.lower()


def test_strategy_contains_helper_signatures():
    text = get_strategy("medium", None)
    assert "find_module" in text
    assert "find_by_type" in text
    assert "extract_procedures" in text
    assert "safe_grep" in text
    assert "read_procedure" in text
    assert "find_callers" in text


def test_disambiguation_section_warns_about_keys_and_paths():
    """BUG-5b/7: DISAMBIGUATION в strategy header содержит:
    - предупреждение о различии ключей get_object_full_structure vs find_attributes;
    - guidance по путям parse_object_xml (директория vs файл);
    - указание find_roles для прав вместо parse_object_xml('Roles/X').
    """
    text = get_strategy("medium", None)
    # DISAMBIGUATION секция присутствует
    assert "DISAMBIGUATION" in text
    # BUG-5b: ключи отличаются от find_attributes
    assert "attr_name" in text
    assert "find_attributes" in text
    # BUG-7: предпочтительный путь parse_object_xml — к директории
    assert "Documents/X" in text
    # BUG-7: для ролей — find_roles, не parse_object_xml
    assert "find_roles" in text


def test_strategy_contains_effort_guidance():
    for effort in ["low", "medium", "high", "max"]:
        text = get_strategy(effort, None)
        # At minimum the strategy should mention the effort level or contain some guidance
        assert len(text) > 100


def test_strategy_with_format_info():
    """When format_info is provided, strategy should mention format."""
    from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat

    cf_info = FormatInfo(
        primary_format=SourceFormat.CF,
        root_path="/test",
        bsl_file_count=100,
        has_configuration_xml=True,
        metadata_categories_found=["CommonModules", "Documents"],
    )
    text = get_strategy("medium", cf_info)
    assert "CF" in text or "cf" in text or "Ext" in text


def test_get_strategy_format_hints():
    """Format-specific hints must appear for CF and EDT, not for None."""
    from rlm_tools_bsl.format_detector import FormatInfo, SourceFormat

    cf_info = FormatInfo(
        primary_format=SourceFormat.CF,
        root_path="/test",
        bsl_file_count=100,
        has_configuration_xml=True,
        metadata_categories_found=[],
    )
    cf_text = get_strategy("medium", cf_info)
    assert "FORMAT: CF" in cf_text
    assert "Ext/" in cf_text

    edt_info = FormatInfo(
        primary_format=SourceFormat.EDT,
        root_path="/test",
        bsl_file_count=50,
        has_configuration_xml=False,
        metadata_categories_found=[],
    )
    edt_text = get_strategy("medium", edt_info)
    assert "FORMAT: EDT" in edt_text

    none_text = get_strategy("medium", None)
    assert "FORMAT: CF" not in none_text
    assert "FORMAT: EDT" not in none_text


# --- Descriptions ---


def test_rlm_start_description():
    assert "BSL" in RLM_START_DESCRIPTION
    assert "1C" in RLM_START_DESCRIPTION
    assert "find_module" in RLM_START_DESCRIPTION


# --- Business recipes ---


def test_business_recipes_structure():
    """All domains must have compact and full keys."""
    # v1.10.0: 9 base + перечисления + ввод на основании + структура объекта = 12
    assert len(_BUSINESS_RECIPES) == 12
    short_full_allowed = {
        "тип реквизита": 3,
        "ссылки": 3,
        "перечисления": 4,
        "ввод на основании": 4,
        "структура объекта": 4,
    }
    for domain, recipe in _BUSINESS_RECIPES.items():
        assert "compact" in recipe, f"{domain}: missing compact"
        assert "full" in recipe, f"{domain}: missing full"
        assert len(recipe["compact"]) >= 2, f"{domain}: compact too short"
        min_full = short_full_allowed.get(domain, 6)
        assert len(recipe["full"]) >= min_full or domain == "интеграция", f"{domain}: full too short"


def test_recipe_aliases_consistency():
    """Each alias must point to an existing _BUSINESS_RECIPES domain."""
    for alias, dom in _RECIPE_ALIASES.items():
        assert dom in _BUSINESS_RECIPES, f"alias '{alias}' points to missing domain '{dom}'"


def test_match_recipe_found():
    assert _match_recipe("Как рассчитывается себестоимость?") == "себестоимость"
    assert _match_recipe("Проведение документа РеализацияТоваров") == "проведение"
    assert _match_recipe("Распределение затрат по номенклатуре") == "распределение"
    assert _match_recipe("Печать товарной накладной") == "печать"
    assert _match_recipe("Права доступа к справочнику") == "права"
    assert _match_recipe("Интеграция с внешними системами") == "интеграция"


def test_match_recipe_aliases():
    assert _match_recipe("обмен данными с сайтом") == "интеграция"
    assert _match_recipe("синхронизация с сайтом") == "интеграция"
    assert _match_recipe("exchange data with external system") == "интеграция"


def test_match_recipe_form_events():
    """'события формы' recipe matches form-related queries."""
    assert _match_recipe("события формы документа") == "события формы"
    assert _match_recipe("обработчики формы справочника") == "события формы"


def test_match_recipe_print_form_not_hijacked():
    """'печатная форма' must NOT match 'события формы' — 'печать' matches first."""
    result = _match_recipe("печатная форма")
    assert result is None or result == "печать"
    # 'печать' domain requires exact substring: "печать" NOT in "печатная форма" (ь≠н)
    # so both None and "печать" are acceptable (depends on substring match)


def test_match_recipe_bare_form_no_match():
    """Bare 'форма' must NOT match any recipe (too broad)."""
    assert _match_recipe("форма ТОРГ-12") is None


def test_match_recipe_not_found():
    assert _match_recipe("Найди все HTTP-сервисы") is None
    assert _match_recipe("") is None
    assert _match_recipe("Покажи структуру модуля") is None


def test_match_recipe_case_insensitive():
    assert _match_recipe("СЕБЕСТОИМОСТЬ товаров") == "себестоимость"
    assert _match_recipe("Печать ТОРГ-12") == "печать"


def test_strategy_step0_always_present():
    text = get_strategy("medium", None)
    assert "Step 0" in text
    assert "UNDERSTAND" in text


def test_strategy_compact_recipe_low_effort():
    text = get_strategy("low", None, query="себестоимость")
    assert "BUSINESS RECIPE: себестоимость" in text
    # Extract only the recipe section
    start = text.index("BUSINESS RECIPE: себестоимость")
    rest = text[start:]
    end = rest.index("\n\n") if "\n\n" in rest else len(rest)
    recipe_section = rest[:end]
    # compact has exactly 3 numbered steps
    assert "  1." in recipe_section
    assert "  3." in recipe_section
    assert "find_by_type" in recipe_section
    assert "find_register_writers" in recipe_section
    # full-only items must NOT be in the recipe section
    assert "find_callers_context" not in recipe_section
    assert "analyze_subsystem" not in recipe_section


def test_strategy_compact_recipe_medium_effort():
    text = get_strategy("medium", None, query="себестоимость")
    assert "BUSINESS RECIPE: себестоимость" in text


def test_strategy_full_recipe_high_effort():
    text = get_strategy("high", None, query="себестоимость")
    assert "BUSINESS RECIPE: себестоимость" in text
    assert "find_callers_context" in text
    assert "analyze_subsystem" in text


def test_strategy_full_recipe_max_effort():
    text = get_strategy("max", None, query="себестоимость")
    assert "BUSINESS RECIPE: себестоимость" in text
    assert "ALT:" in text


def test_strategy_no_recipe_without_query():
    text = get_strategy("high", None, query="")
    assert "BUSINESS RECIPE: " not in text
    # Step 0 generic hint still present
    assert "Step 0" in text


def test_strategy_no_recipe_no_match():
    text = get_strategy("high", None, query="Найди HTTP-сервисы")
    assert "BUSINESS RECIPE: " not in text


def test_strategy_recipe_all_domains():
    """Each domain can be matched and injected."""
    for domain in _BUSINESS_RECIPES:
        text = get_strategy("high", None, query=domain)
        assert f"BUSINESS RECIPE: {domain}" in text


def test_integration_recipe_exists():
    assert "интеграция" in _BUSINESS_RECIPES


def test_integration_recipe_compact():
    recipe = _BUSINESS_RECIPES["интеграция"]["compact"]
    assert len(recipe) >= 3
    assert any("find_http_services" in s for s in recipe)


def test_integration_recipe_full():
    recipe = _BUSINESS_RECIPES["интеграция"]["full"]
    assert len(recipe) >= 6
    assert any("find_web_services" in s for s in recipe)
    assert any("find_xdto_packages" in s for s in recipe)
    assert any("find_exchange_plan_content" in s for s in recipe)


def test_integration_recipe_code_hint():
    recipe = _BUSINESS_RECIPES["интеграция"]
    assert "code_hint" in recipe
    assert "find_http_services" in recipe["code_hint"]
    assert "find_exchange_plan_content" in recipe["code_hint"]
    assert "find_scheduled_jobs" in recipe["code_hint"]


def test_integration_strategy_injection():
    text = get_strategy("high", None, query="интеграция с внешними системами")
    assert "BUSINESS RECIPE" in text
    assert "find_http_services" in text


def test_integration_strategy_code_hint_injected():
    text = get_strategy("high", None, query="интеграция с внешними системами")
    assert "Ready-to-use code" in text
    assert "```python" in text


def test_integration_strategy_via_alias():
    text = get_strategy("high", None, query="обмен данными с сайтом")
    assert "BUSINESS RECIPE: интеграция" in text
