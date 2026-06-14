"""Tests for the git grep full-text search backend (v1.15.0).

Covers the low-level ``_git_grep`` core (pathspec scoping, sanitisation, ``-z``
parsing, truncation, CRLF, untracked coverage, exit codes) and the helper-level
wiring (``git_search`` contract, ``safe_grep`` git acceleration + parity,
registration gating, and the cwd/git-independent doc snapshot).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rlm_tools_bsl.bsl_index as bsl_index_mod
from rlm_tools_bsl.bsl_index import (
    _git_grep,
    _sanitize_grep_excludes,
    _sanitize_grep_file_types,
    _sanitize_grep_path,
)
from rlm_tools_bsl.bsl_helpers import (
    _is_literal_pattern,
    build_helper_metadata_snapshot,
    make_bsl_helpers,
)
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers

# A distinctive, "non-domain" token that only the fixtures contain.
TOK = "VINTOKEN"

MODULE_BSL = f"""\
Процедура ТестоваяПроцедура() Экспорт
    // {TOK} в комментарии общего модуля
    Контрагент = "{TOK}";
КонецПроцедуры
"""

FORM_XML = f"""\
<Form>
    <LabelField name="ТоварыНоменклатура_{TOK}" id="4068">
    <DataPath>Объект.Товары.{TOK}</DataPath>
</Form>
"""

OTHER_DOC_BSL = f"""\
Процедура ПередЗаписью(Отказ)
    Значение = "{TOK}";  // тот же токен в Documents/
КонецПроцедуры
"""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def _git_init(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@test.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


def _make_repo(tmp_path: Path) -> Path:
    """Create a CF-style project under ``tmp_path/src`` and git-init at root.

    base_path = tmp_path/src (a **subdir** of the git root) so we also verify
    paths come back base-relative and the scope does not escape to repo root.
    """
    base = tmp_path / "src"

    cm = base / "CommonModules" / "МойМодуль" / "Ext"
    cm.mkdir(parents=True)
    (cm / "Module.bsl").write_text(MODULE_BSL, encoding="utf-8")

    form = base / "Documents" / "ТестовыйДокумент" / "Ext"
    form.mkdir(parents=True)
    (form / "Form.xml").write_text(FORM_XML, encoding="utf-8")

    other = base / "Documents" / "Другой" / "Ext"
    other.mkdir(parents=True)
    (other / "ObjectModule.bsl").write_text(OTHER_DOC_BSL, encoding="utf-8")

    (base / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    _git_init(tmp_path)
    return base


def _make_bsl(base: Path, **kwargs) -> dict:
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


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path)


# ---------------------------------------------------------------------------
# Sanitisers (pure)
# ---------------------------------------------------------------------------


def test_sanitize_path_valid():
    assert _sanitize_grep_path("CommonModules") == "CommonModules"
    assert _sanitize_grep_path("CommonModules/") == "CommonModules"
    assert _sanitize_grep_path("/CommonModules/") == "CommonModules"
    assert _sanitize_grep_path("a/b/c") == "a/b/c"
    assert _sanitize_grep_path("") == ""  # unset → no filter
    assert _sanitize_grep_path(None) == ""


def test_sanitize_path_rejected():
    # Pathspec magic / escapes, drive, parent, internal empty, all-slashes, globs.
    for bad in (
        ":/",
        ":(top)",
        ":!x",
        "..",
        "a/../b",
        "a//b",
        "C:/Windows",
        "/",
        "///",
        "a\\b",
        "*",
        "CommonModules*",
        "Doc[a-z]",
        "a?b",
    ):
        assert _sanitize_grep_path(bad) is None, bad


def test_sanitize_file_types():
    assert _sanitize_grep_file_types("bsl,xml") == ["bsl", "xml"]
    assert _sanitize_grep_file_types("") == []  # unset → no filter
    assert _sanitize_grep_file_types(["bsl", "mdo"]) == ["bsl", "mdo"]
    # Any invalid extension rejects the whole call (no silent drop).
    assert _sanitize_grep_file_types("bsl;rm") is None
    assert _sanitize_grep_file_types("bsl,.xml") is None
    assert _sanitize_grep_file_types("bsl, x ml") is None


def test_is_literal_pattern():
    assert _is_literal_pattern("VINTOKEN")
    assert _is_literal_pattern("Контрагент")
    assert not _is_literal_pattern("a.*b")
    assert not _is_literal_pattern(r"\d+")
    assert not _is_literal_pattern("foo$")


# ---------------------------------------------------------------------------
# _git_grep core
# ---------------------------------------------------------------------------


def test_git_grep_finds_bsl_and_xml(repo):
    hits = _git_grep(str(repo), TOK, mode="lines")
    files = {h["file"] for h in hits}
    assert any(f.endswith("Module.bsl") for f in files)
    assert any(f.endswith("Form.xml") for f in files)
    # base-relative paths (match modules.rel_path), no repo-root escape.
    for h in hits:
        assert not h["file"].startswith(("..", "/"))
        assert "src/" not in h["file"]
        assert isinstance(h["line"], int) and h["line"] >= 1
        assert "text" in h


def test_git_grep_files_mode(repo):
    hits = _git_grep(str(repo), TOK, mode="files")
    assert hits and all(set(h.keys()) == {"file"} for h in hits)


def test_git_grep_file_types_filter(repo):
    xml_only = _git_grep(str(repo), TOK, file_types="xml", mode="files")
    assert xml_only and all(h["file"].endswith(".xml") for h in xml_only)


def test_git_grep_scoping_no_or_leak(repo):
    """path + file_types must NOT OR-leak *.bsl outside path (Codex crit#1)."""
    hits = _git_grep(str(repo), TOK, path="CommonModules", file_types="bsl", mode="files")
    files = {h["file"] for h in hits}
    assert any("CommonModules" in f for f in files)
    assert not any(f.startswith("Documents/") for f in files)


def test_git_grep_path_as_file(repo):
    """path may be a concrete file when file_types is not given."""
    target = "CommonModules/МойМодуль/Ext/Module.bsl"
    hits = _git_grep(str(repo), TOK, path=target, mode="files")
    assert [h["file"] for h in hits] == [target]
    # Leading/trailing slash is normalised to the same result.
    hits2 = _git_grep(str(repo), TOK, path="/" + target + "/", mode="files")
    assert [h["file"] for h in hits2] == [target]


def test_git_grep_malformed_filters_return_none(repo):
    assert _git_grep(str(repo), TOK, path=":/") is None
    assert _git_grep(str(repo), TOK, path="../escape") is None
    assert _git_grep(str(repo), TOK, path="/") is None  # all-slashes → error
    assert _git_grep(str(repo), TOK, path="*") is None  # glob → no silent widening
    assert _git_grep(str(repo), TOK, path="CommonModules*", file_types="bsl") is None
    assert _git_grep(str(repo), TOK, file_types="bsl;rm") is None


def test_git_grep_path_empty_is_whole_config(repo):
    """Empty path = whole config (valid); only non-empty all-slash collapses."""
    hits = _git_grep(str(repo), TOK, path="", mode="files")
    assert len(hits) >= 3  # bsl + xml + other doc bsl


def test_git_grep_multipattern_guard(repo):
    assert _git_grep(str(repo), "a\nb") is None
    assert _git_grep(str(repo), "a\x00b") is None
    assert _git_grep(str(repo), "") is None


def test_git_grep_no_match_is_empty_not_none(repo):
    assert _git_grep(str(repo), "ZZZ_NO_SUCH_TOKEN_QQQ") == []


def test_git_grep_leading_dash_pattern(repo):
    # Pattern starting with '-' goes through '-e' and must not be parsed as a flag.
    assert _git_grep(str(repo), "-NoSuchDashThing") == []


def test_git_grep_ignore_case(repo):
    assert _git_grep(str(repo), TOK.lower(), ignore_case=True, mode="files")
    assert _git_grep(str(repo), TOK.lower(), ignore_case=False, mode="files") == []


def test_git_grep_regex(repo):
    # ASCII regex: a single '.' matches one byte (a multibyte Cyrillic '.' would
    # not, since git grep matches bytes — that's a deliberately ASCII pattern).
    hits = _git_grep(str(repo), "VIN.OKEN", regex=True, mode="files")
    assert any(h["file"].endswith("Module.bsl") for h in hits)


def test_git_grep_truncation_sentinel(repo):
    hits = _git_grep(str(repo), TOK, mode="lines", max_results=1, include_truncation_sentinel=True)
    assert len(hits) == 2
    assert hits[-1] == {"_truncated": True, "shown": 1}
    # Without the flag: hard cut, no sentinel.
    plain = _git_grep(str(repo), TOK, mode="lines", max_results=1, include_truncation_sentinel=False)
    assert len(plain) == 1 and "_truncated" not in plain[0]


def test_git_grep_max_per_file(repo):
    # Module.bsl has 2 occurrences; max_per_file=1 caps it to one.
    target = "CommonModules/МойМодуль/Ext/Module.bsl"
    full = _git_grep(str(repo), TOK, path=target, max_per_file=0)
    capped = _git_grep(str(repo), TOK, path=target, max_per_file=1)
    assert len(full) == 2
    assert len(capped) == 1


def test_git_grep_literal_files_only(repo):
    target = "CommonModules/МойМодуль/Ext/Module.bsl"
    hits = _git_grep(str(repo), TOK, literal_files=[target], mode="files")
    assert [h["file"] for h in hits] == [target]
    # Empty candidate list → nothing to search (not whole config).
    assert _git_grep(str(repo), TOK, literal_files=[]) == []


def test_git_grep_base_is_subdir_relative(repo):
    """Paths are relative to base_path (subdir), not the git root."""
    hits = _git_grep(str(repo), TOK, mode="files")
    assert all(h["file"].startswith(("CommonModules/", "Documents/")) for h in hits)


# ---------------------------------------------------------------------------
# Untracked / dirty / gitignore coverage
# ---------------------------------------------------------------------------


def test_untracked_and_dirty_and_ignored(repo):
    root = repo.parent
    # New untracked file (not added to git) → found via --untracked.
    new = repo / "CommonModules" / "Новый" / "Ext"
    new.mkdir(parents=True)
    (new / "Module.bsl").write_text(f"// {TOK}_UNTRACKED\n", encoding="utf-8")
    # Unstaged edit to a tracked file → visible without commit.
    mod = repo / "CommonModules" / "МойМодуль" / "Ext" / "Module.bsl"
    mod.write_text(MODULE_BSL + f"\n// {TOK}_UNSTAGED\n", encoding="utf-8")
    # .gitignore'd file → intentionally NOT searched.
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    ign = repo / "ignored"
    ign.mkdir()
    (ign / "x.bsl").write_text(f"// {TOK}_IGNORED\n", encoding="utf-8")

    text_blob = "\n".join(h["text"] for h in _git_grep(str(repo), TOK))
    assert f"{TOK}_UNTRACKED" in text_blob
    assert f"{TOK}_UNSTAGED" in text_blob
    assert f"{TOK}_IGNORED" not in text_blob


# ---------------------------------------------------------------------------
# CRLF behaviour
# ---------------------------------------------------------------------------


def test_crlf_literal_and_anchor(repo):
    """Fixture written with explicit CRLF (not relying on checkout/autocrlf)."""
    d = repo / "CommonModules" / "CRLFМод" / "Ext"
    d.mkdir(parents=True)
    f = d / "Module.bsl"
    f.write_bytes("Процедура П()\r\n    VINTOKEN_EOL\r\nКонецПроцедуры\r\n".encode("utf-8"))

    # Literal mid-/end-line match is CRLF-transparent; output has no trailing CR.
    hits = _git_grep(str(repo), "VINTOKEN_EOL", path="CommonModules/CRLFМод/Ext/Module.bsl")
    assert len(hits) == 1
    assert hits[0]["text"] == "VINTOKEN_EOL"  # .strip() removed the leading ws + trailing \r

    # End-of-line anchor: bare '$' fails on CRLF (trailing CR sits before EOL);
    # '[[:space:]]*$' tolerates it. NOTE: git's POSIX ERE does NOT treat '\r' as
    # a carriage return (it's a literal 'r'), so '\r?$' would NOT work here.
    p = "CommonModules/CRLFМод/Ext/Module.bsl"
    assert _git_grep(str(repo), "VINTOKEN_EOL$", regex=True, path=p) == []
    anchored = _git_grep(str(repo), "VINTOKEN_EOL[[:space:]]*$", regex=True, path=p)
    assert len(anchored) == 1


@pytest.mark.skipif(os.name == "nt", reason="':' is illegal in Windows filenames")
def test_git_grep_z_parsing_colon_in_path(tmp_path):
    """-z parses fields by NUL, so a ':' in a path doesn't break parsing."""
    base = tmp_path / "src"
    weird = base / "a:b"
    weird.mkdir(parents=True)
    (weird / "m.bsl").write_text(f"// {TOK}\n", encoding="utf-8")
    (base / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    _git_init(tmp_path)
    hits = _git_grep(str(base), TOK, mode="files")
    assert any("a:b" in h["file"] for h in hits)


# ---------------------------------------------------------------------------
# git_search helper-level contract
# ---------------------------------------------------------------------------


def test_git_search_registered_and_finds_xml(repo):
    bsl = _make_bsl(repo)
    assert "git_search" in bsl["_registry"]
    hits = bsl["git_search"](TOK, file_types="xml")
    assert any(h["file"].endswith("Form.xml") for h in hits)


def test_git_search_error_dict_on_failure(repo, monkeypatch):
    bsl = _make_bsl(repo)
    monkeypatch.setattr(bsl_index_mod, "_git_grep", lambda *a, **k: None)
    out = bsl["git_search"](TOK)
    assert out == [{"error": "git grep failed or timed out"}]


def test_git_search_error_dict_on_bad_filter(repo):
    bsl = _make_bsl(repo)
    assert bsl["git_search"](TOK, path=":/") == [{"error": "git grep failed or timed out"}]


def test_git_search_truncation_contract(repo):
    bsl = _make_bsl(repo)
    hits = bsl["git_search"](TOK, max_results=1)
    assert hits[-1].get("_truncated") is True


# ---------------------------------------------------------------------------
# safe_grep: strict contract + git parity
# ---------------------------------------------------------------------------


def test_safe_grep_strict_contract_no_sentinel(repo):
    bsl = _make_bsl(repo)
    results = bsl["safe_grep"](TOK, max_files=50)
    assert results  # found something
    for r in results:
        assert set(r.keys()) == {"file", "line", "text"}


def test_safe_grep_git_parity_literal(repo, monkeypatch):
    """safe_grep literal results identical with and without the git backend."""
    bsl_git = _make_bsl(repo)
    with_git = bsl_git["safe_grep"](TOK, max_files=50)

    # Force the no-git path by making availability return False on a fresh closure.
    bsl_nogit = _make_bsl(repo)
    monkeypatch.setattr(bsl_index_mod, "_git_available", lambda _p: False)
    # New closure's cache is unset; the patched _git_available is imported lazily.
    without_git = bsl_nogit["safe_grep"](TOK, max_files=50)

    def _key(rs):
        return sorted((r["file"], r["line"], r["text"]) for r in rs)

    assert _key(with_git) == _key(without_git)


def test_safe_grep_regex_stays_on_python(repo):
    """A regex pattern (metachars) must still work via safe_grep (Python re)."""
    bsl = _make_bsl(repo)
    results = bsl["safe_grep"]("Контр.гент", max_files=50)
    assert any(r["file"].endswith("Module.bsl") for r in results)


# ---------------------------------------------------------------------------
# Registration gating
# ---------------------------------------------------------------------------


def test_no_git_project_excludes_git_search(tmp_path):
    """A project with no .git: git_search absent, safe_grep still works."""
    base = tmp_path / "src"
    cm = base / "CommonModules" / "М" / "Ext"
    cm.mkdir(parents=True)
    (cm / "Module.bsl").write_text(MODULE_BSL, encoding="utf-8")
    (base / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    bsl = _make_bsl(base)
    assert "git_search" not in bsl["_registry"]
    assert bsl["safe_grep"](TOK, max_files=10)  # graceful Python path


def test_git_not_installed_excludes_git_search(repo, monkeypatch):
    monkeypatch.setattr(bsl_index_mod, "_find_git", lambda: None)
    bsl = _make_bsl(repo)
    assert "git_search" not in bsl["_registry"]
    # safe_grep falls back to Python without raising.
    assert bsl["safe_grep"](TOK, max_files=50)


def test_register_git_search_never(repo):
    bsl = _make_bsl(repo, register_git_search="never")
    assert "git_search" not in bsl["_registry"]


def test_register_git_search_force_without_git(tmp_path):
    """force registers git_search even with no .git (used by the doc snapshot)."""
    base = tmp_path / "src"
    base.mkdir(parents=True)
    (base / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    bsl = _make_bsl(base, register_git_search="force")
    assert "git_search" in bsl["_registry"]


def test_snapshot_documents_git_search_regardless_of_cwd():
    snap = build_helper_metadata_snapshot()
    assert "git_search" in snap
    assert snap["git_search"]["cat"]
    assert snap["git_search"]["recipe"]


# ---------------------------------------------------------------------------
# Strategy routing note
# ---------------------------------------------------------------------------


def test_strategy_routing_note_gated_by_registry():
    from rlm_tools_bsl.bsl_knowledge import _git_search_routing

    assert _git_search_routing(None) == ""
    assert _git_search_routing({"safe_grep": {}}) == ""
    note = _git_search_routing({"git_search": {}})
    assert "git_search" in note and "FULL-TEXT SEARCH" in note


# ---------------------------------------------------------------------------
# exclude_path (v1.20.0)
# ---------------------------------------------------------------------------


def _add_nested_form(repo: Path, tok: str = TOK) -> str:
    """A form XML nested under a ``Forms/`` segment at depth (not top-level)."""
    d = repo / "Documents" / "ДокС" / "Forms" / "ФормаС" / "Ext"
    d.mkdir(parents=True)
    (d / "Form.xml").write_text(f"<Form><DataPath>{tok}</DataPath></Form>\n", encoding="utf-8")
    return "Documents/ДокС/Forms/ФормаС/Ext/Form.xml"


def test_sanitize_excludes():
    assert _sanitize_grep_excludes("") == []
    assert _sanitize_grep_excludes(None) == []
    assert _sanitize_grep_excludes("Forms") == ["Forms"]
    assert _sanitize_grep_excludes("Forms,Templates") == ["Forms", "Templates"]
    assert _sanitize_grep_excludes(["Forms", "Templates"]) == ["Forms", "Templates"]
    assert _sanitize_grep_excludes("Forms, Templates ,ConfigDumpInfo.xml") == [
        "Forms",
        "Templates",
        "ConfigDumpInfo.xml",
    ]
    # Any malformed element rejects the whole call (no silent narrowing-away).
    for bad in ("../x", "a*", "Forms,a*", ":(top)", "C:/Win", "a\\b", "/"):
        assert _sanitize_grep_excludes(bad) is None, bad


def test_git_grep_exclude_nested_forms(repo):
    """Codex #2: a nested ``*/Forms/*`` must be excluded (a magic-free literal
    ``:(exclude)Forms`` would NOT drop it — it anchors at the repo root)."""
    nested = _add_nested_form(repo)
    base = {h["file"] for h in _git_grep(str(repo), TOK, mode="files")}
    assert nested in base  # present without exclude
    excluded = {h["file"] for h in _git_grep(str(repo), TOK, mode="files", exclude_path="Forms")}
    assert nested not in excluded  # dropped at depth
    # Non-Forms matches survive…
    assert any(f.endswith("Module.bsl") for f in excluded)
    # …including a top-level Form.xml that is NOT under a Forms/ dir (segment-exact,
    # not a "Form" substring match).
    assert any(f.endswith("ТестовыйДокумент/Ext/Form.xml") for f in excluded)


def test_git_grep_exclude_whole_config(repo):
    """exclude over the whole config (no positive path) still applies (the
    internal positive '.' makes git's exclude magic subtract from everything)."""
    nested = _add_nested_form(repo)
    out = {h["file"] for h in _git_grep(str(repo), TOK, mode="files", exclude_path="Forms")}
    assert out and nested not in out


def test_git_grep_exclude_with_path(repo):
    nested = _add_nested_form(repo)
    out = {h["file"] for h in _git_grep(str(repo), TOK, path="Documents", mode="files", exclude_path="Forms")}
    assert nested not in out
    assert out and all(f.startswith("Documents/") for f in out)


def test_git_grep_exclude_with_file_types(repo):
    _add_nested_form(repo)
    out = {h["file"] for h in _git_grep(str(repo), TOK, file_types="xml", mode="files", exclude_path="Forms")}
    assert out and all(f.endswith(".xml") for f in out)
    assert not any("/Forms/" in f for f in out)


def test_git_grep_exclude_file_at_any_depth(repo):
    """A bare filename excludes that file at any depth (e.g. ConfigDumpInfo.xml)."""
    d = repo / "Sub" / "Deep"
    d.mkdir(parents=True)
    (d / "ConfigDumpInfo.xml").write_text(f"<x>{TOK}</x>\n", encoding="utf-8")
    full = {h["file"] for h in _git_grep(str(repo), TOK, mode="files")}
    assert "Sub/Deep/ConfigDumpInfo.xml" in full
    out = {h["file"] for h in _git_grep(str(repo), TOK, mode="files", exclude_path="ConfigDumpInfo.xml")}
    assert out and "Sub/Deep/ConfigDumpInfo.xml" not in out


def test_git_grep_exclude_multiple(repo):
    nested = _add_nested_form(repo)
    tdir = repo / "Documents" / "ДокТ" / "Templates" / "Макет" / "Ext"
    tdir.mkdir(parents=True)
    (tdir / "Template.xml").write_text(f"<x>{TOK}</x>\n", encoding="utf-8")
    tpath = "Documents/ДокТ/Templates/Макет/Ext/Template.xml"
    out = {h["file"] for h in _git_grep(str(repo), TOK, mode="files", exclude_path="Forms,Templates")}
    assert out and nested not in out and tpath not in out


def test_git_grep_exclude_malformed_returns_none(repo):
    assert _git_grep(str(repo), TOK, exclude_path="../x") is None
    assert _git_grep(str(repo), TOK, exclude_path="a*") is None
    assert _git_grep(str(repo), TOK, exclude_path="Forms,:(top)") is None


def test_git_grep_literal_files_ignores_exclude(repo):
    """The literal_files branch is exact; exclude_path is NOT applied there, so
    safe_grep (which always uses literal_files) is unaffected by exclude."""
    nested = _add_nested_form(repo)
    hits = _git_grep(str(repo), TOK, literal_files=[nested], mode="files", exclude_path="Forms")
    assert [h["file"] for h in hits] == [nested]


def test_git_grep_literal_files_ignores_even_malformed_exclude(repo):
    """exclude_path is sanitised/applied ONLY on the non-literal_files branch, so
    even a malformed exclude_path must be ignored (not → None) with literal_files."""
    target = "CommonModules/МойМодуль/Ext/Module.bsl"
    hits = _git_grep(str(repo), TOK, literal_files=[target], mode="files", exclude_path="a*")
    assert [h["file"] for h in hits] == [target]  # malformed exclude ignored, not an error


def test_git_search_exclude_path_helper(repo):
    bsl = _make_bsl(repo)
    nested = _add_nested_form(repo)
    out = bsl["git_search"](TOK, exclude_path="Forms")
    files = {h.get("file") for h in out if "file" in h}
    assert nested not in files
    assert any(str(f).endswith("Module.bsl") for f in files)


def test_git_search_exclude_malformed_error(repo):
    bsl = _make_bsl(repo)
    assert bsl["git_search"](TOK, exclude_path="a*") == [{"error": "git grep failed or timed out"}]


def test_git_search_positional_compat_unchanged(repo):
    """exclude_path added at the END → the 4th positional is still ``regex`` (Codex #1)."""
    bsl = _make_bsl(repo)
    hits = bsl["git_search"]("VIN.OKEN", "CommonModules", "bsl", True, mode="files")
    assert any(str(h.get("file", "")).endswith("Module.bsl") for h in hits)
    # Same positional call with regex=False → the metachar pattern matches nothing literally.
    assert bsl["git_search"]("VIN.OKEN", "CommonModules", "bsl", False, mode="files") == []
