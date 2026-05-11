"""Tests for v1.12.0 multi-line procedure signatures.

Covers ``bsl_knowledge._merge_proc_continuations`` + parser parity between
``bsl_index._parse_procedures_from_lines`` and the live helper
``bsl_helpers._parse_procedures``.

Also pins the ``extract_procedures`` opportunistic live-fill behavior on
indexed paths where the index missed a multi-line procedure.
"""

from __future__ import annotations

import os
import textwrap
from typing import Any

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import _parse_procedures_from_lines
from rlm_tools_bsl.bsl_knowledge import _merge_proc_continuations
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


_MULTI_BSL = textwrap.dedent("""\
    Процедура OneLine(a, b)
        Сообщить("");
    КонецПроцедуры

    Процедура TwoLine(a,
        b)
        Сообщить("");
    КонецПроцедуры

    Процедура FourLine(a,
        b,
        c,
        d) Экспорт
        Возврат a + b;
    КонецФункции
""")


_LITERAL_PAREN_BSL = textwrap.dedent("""\
    Процедура SignatureWithStringParen(Знач X = "(не скобка)",
        Y)
        Возврат X;
    КонецПроцедуры
""")


_UNCLOSED_BSL = textwrap.dedent("""\
    Процедура UnclosedSig(a,
        b,
        c,
        d,
        e,
        f,
        g
""")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


@pytest.fixture()
def helpers(tmp_path):
    cf = os.path.join(str(tmp_path), "src", "cf")
    _write(
        os.path.join(cf, "Configuration.xml"),
        "<MetaDataObject><Configuration><Properties><Name>X</Name></Properties></Configuration></MetaDataObject>",
    )
    _write(os.path.join(cf, "CommonModules", "M", "Ext", "Module.bsl"), _MULTI_BSL)
    _write(os.path.join(cf, "CommonModules", "L", "Ext", "Module.bsl"), _LITERAL_PAREN_BSL)
    _write(os.path.join(cf, "CommonModules", "U", "Ext", "Module.bsl"), _UNCLOSED_BSL)

    generic, resolve_safe = make_helpers(cf)
    fmt = detect_format(cf)
    bsl = make_bsl_helpers(
        base_path=cf,
        resolve_safe=resolve_safe,
        read_file_fn=generic["read_file"],
        grep_fn=generic["grep"],
        glob_files_fn=generic["glob_files"],
        format_info=fmt,
        idx_reader=None,
    )
    return bsl, cf


class TestMergeContinuations:
    def test_single_line_unchanged(self):
        lines = ["Процедура F() Экспорт", "    Возврат 1;", "КонецФункции"]
        merged, line_map = _merge_proc_continuations(lines)
        assert merged == lines
        assert line_map == [1, 2, 3]

    def test_two_line_signature_merged(self):
        lines = ["Процедура F(a,", "    b)", "    Возврат 1;", "КонецПроцедуры"]
        merged, line_map = _merge_proc_continuations(lines)
        assert len(merged) == 3
        assert merged[0].startswith("Процедура F(a,")
        assert "b)" in merged[0]
        assert line_map == [1, 3, 4]

    def test_four_line_signature_merged(self):
        lines = ["Процедура F(a,", "  b,", "  c,", "  d)", "    Возврат a;", "КонецПроцедуры"]
        merged, line_map = _merge_proc_continuations(lines)
        assert len(merged) == 3
        assert "d)" in merged[0]
        assert line_map[0] == 1

    def test_string_literal_paren_not_counted(self):
        lines = ['Процедура F(Знач X = "(не скобка)",', "    Y)", "    Возврат X;", "КонецПроцедуры"]
        merged, line_map = _merge_proc_continuations(lines)
        # Must collapse to single signature line (one '(' in code, ')' on next line balances).
        assert len(merged) == 3
        assert "Y)" in merged[0]


class TestIndexerParser:
    def test_two_line_signature_indexer(self):
        lines = _MULTI_BSL.splitlines()
        procs = _parse_procedures_from_lines(lines)
        names = {p["name"]: p for p in procs}
        assert "OneLine" in names and "TwoLine" in names and "FourLine" in names
        assert names["TwoLine"]["line"] == 5
        assert names["FourLine"]["line"] == 10
        assert names["FourLine"]["is_export"] is True

    def test_unclosed_signature_does_not_hang(self):
        lines = _UNCLOSED_BSL.splitlines()
        # Must terminate (hard-cap) and not blow up.
        procs = _parse_procedures_from_lines(lines)
        # No КонецПроцедуры — the parser treats it as unclosed (end at EOF) or skips.
        # Either way, the call must return quickly without exceptions.
        assert isinstance(procs, list)


class TestLiveParser:
    def test_parse_procedures_multiline_in_live_helper(self, helpers):
        bsl, _cf = helpers
        procs = bsl["extract_procedures"]("CommonModules/M/Ext/Module.bsl")
        by_name = {p["name"]: p for p in procs}
        assert "TwoLine" in by_name
        assert "FourLine" in by_name
        assert by_name["TwoLine"]["line"] == 5
        # end_line points to КонецПроцедуры (8 for TwoLine)
        assert by_name["TwoLine"]["end_line"] > by_name["TwoLine"]["line"]
        assert by_name["FourLine"]["line"] == 10

    def test_parse_procedures_multiline_matches_indexer(self, helpers):
        bsl, _cf = helpers
        live_procs = bsl["extract_procedures"]("CommonModules/M/Ext/Module.bsl")
        with open(os.path.join(_cf, "CommonModules/M/Ext/Module.bsl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        idx_procs = _parse_procedures_from_lines(lines)
        # Same names + same line numbers
        live_view = [(p["name"], p["line"], p["end_line"]) for p in live_procs]
        idx_view = [(p["name"], p["line"], p["end_line"]) for p in idx_procs]
        assert live_view == idx_view

    def test_read_procedure_multiline_returns_full_body(self, helpers):
        bsl, _cf = helpers
        body = bsl["read_procedure"]("CommonModules/M/Ext/Module.bsl", "FourLine")
        assert body is not None
        assert "Процедура FourLine(a," in body
        assert "Возврат a + b;" in body


class _FakeReader:
    """Minimal stub mimicking the IndexReader contract used by extract_procedures."""

    def __init__(self, methods=None, overrides=None):
        self._methods = methods or []
        self._overrides = overrides or {}

    def get_methods_by_path(self, path):  # noqa: D401
        return list(self._methods)

    def get_overrides_for_path(self, path):
        return dict(self._overrides)

    @property
    def has_fts(self) -> bool:
        return False

    @property
    def has_calls(self) -> bool:
        return False

    def get_all_modules(self):  # noqa: D401
        return []

    def search_methods(self, *_args, **_kwargs):  # pragma: no cover
        return []

    def search_objects(self, *_args, **_kwargs):  # pragma: no cover
        return None

    def search_regions(self, *_args, **_kwargs):  # pragma: no cover
        return None

    def search_module_headers(self, *_args, **_kwargs):  # pragma: no cover
        return None

    def get_object_attributes(self, **_kwargs):  # pragma: no cover
        return None

    def get_predefined_items(self, **_kwargs):  # pragma: no cover
        return None

    def get_statistics(self):  # pragma: no cover
        return {}


def _make_bsl_with_fake_reader(cf: str, reader: Any):
    generic, resolve_safe = make_helpers(cf, idx_reader=reader)
    fmt = detect_format(cf)
    return make_bsl_helpers(
        base_path=cf,
        resolve_safe=resolve_safe,
        read_file_fn=generic["read_file"],
        grep_fn=generic["grep"],
        glob_files_fn=generic["glob_files"],
        format_info=fmt,
        idx_reader=reader,
    )


class TestExtractProceduresLiveFill:
    def test_extract_procedures_live_fill_on_indexed_path(self, helpers):
        """idx_reader returns a partial list — multi-line procedure missing.

        ``extract_procedures`` must append it via live-fill.
        """
        bsl, cf = helpers
        path = "CommonModules/M/Ext/Module.bsl"
        # idx_reader returns only OneLine; TwoLine + FourLine come from live-fill.
        reader = _FakeReader(
            methods=[
                {
                    "name": "OneLine",
                    "type": "Процедура",
                    "line": 1,
                    "end_line": 3,
                    "is_export": False,
                    "params": "a, b",
                }
            ]
        )
        bsl2 = _make_bsl_with_fake_reader(cf, reader)
        procs = bsl2["extract_procedures"](path)
        names = [p["name"] for p in procs]
        assert "OneLine" in names
        assert "TwoLine" in names
        assert "FourLine" in names

    def test_extract_procedures_dedup_index_and_live(self, helpers):
        """If a procedure is present in both index and live, it is not duplicated."""
        bsl, cf = helpers
        path = "CommonModules/M/Ext/Module.bsl"
        reader = _FakeReader(
            methods=[
                {
                    "name": "OneLine",
                    "type": "Процедура",
                    "line": 1,
                    "end_line": 3,
                    "is_export": False,
                    "params": "a, b",
                },
                {
                    "name": "TwoLine",
                    "type": "Процедура",
                    "line": 5,
                    "end_line": 8,
                    "is_export": False,
                    "params": "a, b",
                },
                {
                    "name": "FourLine",
                    "type": "Функция",
                    "line": 10,
                    "end_line": 15,
                    "is_export": True,
                    "params": "a, b, c, d",
                },
            ]
        )
        bsl2 = _make_bsl_with_fake_reader(cf, reader)
        procs = bsl2["extract_procedures"](path)
        names = [p["name"] for p in procs]
        assert names.count("TwoLine") == 1
        assert names.count("FourLine") == 1

    def test_extract_procedures_live_fill_preserves_overrides(self, helpers):
        """Critical: live-fill must respect overrides_map for the freshly added procedures."""
        bsl, cf = helpers
        path = "CommonModules/M/Ext/Module.bsl"
        reader = _FakeReader(
            methods=[
                {
                    "name": "OneLine",
                    "type": "Процедура",
                    "line": 1,
                    "end_line": 3,
                    "is_export": False,
                    "params": "a, b",
                }
            ],
            overrides={
                "FourLine": [
                    {
                        "annotation": "После",
                        "extension_name": "ExtX",
                        "extension_method": "ext_FourLine",
                        "extension_root": "/ext/root",
                        "ext_module_path": "CommonModules/M/Ext/Module.bsl",
                        "ext_line": 1,
                    }
                ]
            },
        )
        bsl2 = _make_bsl_with_fake_reader(cf, reader)
        procs = bsl2["extract_procedures"](path)
        fl = next(p for p in procs if p["name"] == "FourLine")
        assert "overridden_by" in fl, fl
        assert fl["overridden_by"][0]["annotation"] == "После"
        assert fl["overridden_by"][0]["extension_method"] == "ext_FourLine"
