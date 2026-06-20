"""Часть 3: точные подсказки на NameError / неверный kwarg (юнит + execute)."""

from __future__ import annotations

import os
import tempfile

from rlm_tools_bsl.sandbox import Sandbox


# ── Юнит: _add_error_hints напрямую ─────────────────────────────────────────


def test_nameerror_known_alias_suggests_real_helper():
    error = "NameError: name 'get_method_source' is not defined"
    code = "src = get_method_source('Documents/X/Ext/ObjectModule.bsl', 'ПередЗаписью')"
    out = Sandbox._add_error_hints(error, code, available_names={"read_procedure", "find_module"})
    assert "read_procedure" in out


def test_nameerror_fuzzy_suggests_close_helper():
    error = "NameError: name 'find_modul' is not defined"
    code = "m = find_modul('РеализацияТоваровУслуг')"
    out = Sandbox._add_error_hints(error, code, available_names={"find_module", "find_by_type"})
    assert "find_module" in out


def test_nameerror_unknown_falls_back_to_help():
    error = "NameError: name 'совершенно_левое_имя' is not defined"
    code = "x = совершенно_левое_имя()"
    out = Sandbox._add_error_hints(error, code, available_names={"read_procedure"})
    assert "help()" in out


def test_nameerror_backward_compatible_two_args():
    out = Sandbox._add_error_hints("NameError: name 'foo' is not defined", "foo()")
    assert "HINT" in out


# ── v1.23.0: three recurrent agent-shape errors ─────────────────────────────


def test_list_get_attribute_error_hint():
    error = "AttributeError: 'list' object has no attribute 'get'"
    code = "roles = find_roles('X'); print(roles.get('roles'))"
    out = Sandbox._add_error_hints(error, code)
    assert "СПИСОК (list[dict])" in out
    assert "итерируй" in out.lower()


def test_unhashable_dict_set_hint():
    error = "TypeError: unhashable type: 'dict'"
    code = "uniq = set(find_event_subscriptions('X'))"
    out = Sandbox._add_error_hints(error, code)
    assert "unhashable type 'dict'" in out
    assert "set()" in out


def test_keyerror_contract_shape_hint():
    error = "KeyError: 'code_registers'"
    code = "m = find_register_movements('X'); print(m['code_registers'][0]['lines'])"
    out = Sandbox._add_error_hints(error, code)
    assert "KeyError 'code_registers'" in out
    assert "result.keys()" in out


def test_keyerror_does_not_fire_generic_on_full_structure():
    """The get_object_full_structure-specific KeyError hint stays; the generic one is suppressed."""
    error = "KeyError: 'attr_kind'"
    code = "s = get_object_full_structure('X'); print(s['attributes'][0]['attr_kind'])"
    out = Sandbox._add_error_hints(error, code)
    # specific hint mentions attr_kind contract; generic 'result.keys()' must NOT be appended
    assert "attr_kind" in out
    assert "result.keys()" not in out


def test_keyerror_slice_uses_slice_hint_not_generic():
    error = "KeyError: slice(None, 5, None)"
    code = "flow = analyze_document_flow('X'); print(flow[:5])"
    out = Sandbox._add_error_hints(error, code)
    assert "срезаете dict как список" in out
    assert "result.keys()" not in out


# ── Интеграция: реальный Sandbox.execute() пробрасывает _registry/имена ──────


def _cf_sandbox(tmpdir: str) -> Sandbox:
    """Sandbox с минимальной CF-структурой → format_info=cf → есть _registry и read_procedure."""
    os.makedirs(os.path.join(tmpdir, "CommonModules", "М", "Ext"), exist_ok=True)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w", encoding="utf-8") as f:
        f.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject><Configuration><Properties>'
            "<Name>Тест</Name></Properties></Configuration></MetaDataObject>\n"
        )
    with open(os.path.join(tmpdir, "CommonModules", "М", "Ext", "Module.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура П() КонецПроцедуры\n")
    from rlm_tools_bsl.format_detector import detect_format

    return Sandbox(base_path=tmpdir, format_info=detect_format(tmpdir))


def test_execute_nameerror_alias_end_to_end():
    """execute() реально подставляет available_names → подсказка read_procedure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _cf_sandbox(tmpdir)
        res = sb.execute("x = get_method_source('a', 'b')")
        assert res.error is not None
        assert "read_procedure" in res.error


# ── Task 3.2: неверный kwarg → реальная сигнатура ───────────────────────────


def test_unexpected_kwarg_names_real_signature_from_registry():
    error = "TypeError: find_callers_context() got an unexpected keyword argument 'depth'"
    code = "r = find_callers_context('Проц', depth=3)"
    registry = {"find_callers_context": {"sig": "find_callers_context(proc, module_hint, 0, 50) -> {callers, _meta}"}}
    out = Sandbox._add_error_hints(error, code, registry=registry)
    assert "find_callers_context(proc, module_hint" in out


def test_unexpected_kwarg_safe_grep_keeps_specific_hint():
    error = "TypeError: safe_grep() got an unexpected keyword argument 'path'"
    code = "r = safe_grep('X', path='Y')"
    out = Sandbox._add_error_hints(error, code)
    assert "name_hint" in out


def test_execute_unexpected_kwarg_end_to_end():
    """Реальный BSL-helper с неверным kwarg → сигнатура из _registry в error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _cf_sandbox(tmpdir)
        res = sb.execute("find_module('Тест', bogus_kw=1)")
        assert res.error is not None
        assert "find_module(" in res.error  # сигнатура из реестра


def test_execute_generic_grep_wrong_kwarg_end_to_end():
    """round-10: generic-IO helper (grep) НЕ в _registry, но kwarg-хинт всё равно
    даёт его сигнатуру (в бенч-логах был тупик grep(..., limit=...))."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _cf_sandbox(tmpdir)
        res = sb.execute("grep('Шаблон', limit=30)")
        assert res.error is not None
        assert "grep(pattern, path=" in res.error  # сигнатура generic-хелпера
