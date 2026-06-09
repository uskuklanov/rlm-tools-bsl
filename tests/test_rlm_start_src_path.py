"""Integration tests for rlm_start container-path auto-detection (issue #11)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path


_CF_MAIN_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
        <Configuration uuid="00000000-0000-0000-0000-000000000001">
            <Properties>
                <Name>MainCfg</Name>
                <NamePrefix/>
            </Properties>
        </Configuration>
    </MetaDataObject>
""")


def _cf_extension_xml(name: str = "Ext1") -> str:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
            <Configuration uuid="00000000-0000-0000-0000-000000000099">
                <Properties>
                    <Name>{name}</Name>
                    <ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>
                    <NamePrefix>ext_</NamePrefix>
                </Properties>
            </Configuration>
        </MetaDataObject>
    """)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_src_proj(root: Path) -> Path:
    src = root / "src"
    _write(src / "cf" / "Configuration.xml", _CF_MAIN_XML)
    _write(src / "cfe" / "Ext1" / "Configuration.xml", _cf_extension_xml("Ext1"))
    return src


def test_rlm_start_accepts_src_parent(tmp_path):
    from rlm_tools_bsl.server import _rlm_start, _rlm_end

    src = _make_src_proj(tmp_path)
    result = _rlm_start(path=str(src), query="test")
    data = json.loads(result)
    assert "session_id" in data
    assert "resolved_path" in data
    assert Path(data["resolved_path"]) == (src / "cf").resolve()
    assert data["extension_context"]["config_role"] == "main"
    nearby_names = {e["name"] for e in data["extension_context"]["nearby_extensions"]}
    assert "Ext1" in nearby_names
    # v1.19.0: rlm_start surfaces only the override COUNT, not the full per-override
    # dump (the inline dump was unused noise, ~30K on extension-heavy configs).
    ec = data["extension_context"]
    for e in ec["nearby_extensions"]:
        assert isinstance(e.get("overrides_count"), int)
        assert "overrides" not in e
    assert "own_overrides_count" in ec
    assert "own_overrides" not in ec
    _rlm_end(data["session_id"])


def test_rlm_start_multiple_main_returns_error(tmp_path):
    from rlm_tools_bsl.server import _rlm_start

    src = tmp_path / "src"
    _write(src / "main1" / "Configuration.xml", _CF_MAIN_XML)
    _write(src / "main2" / "Configuration.xml", _CF_MAIN_XML)

    result = _rlm_start(path=str(src), query="test")
    data = json.loads(result)
    assert "error" in data
    assert "main_candidates" in data
    names = {Path(c["path"]).name for c in data["main_candidates"]}
    assert names == {"main1", "main2"}


def test_rlm_start_backward_compat_cf_path(tmp_path):
    """Passing .../src/cf directly must behave the same as before."""
    from rlm_tools_bsl.server import _rlm_start, _rlm_end

    src = _make_src_proj(tmp_path)
    cf = src / "cf"
    result = _rlm_start(path=str(cf), query="test")
    data = json.loads(result)
    assert "session_id" in data
    assert Path(data["resolved_path"]) == cf.resolve()
    _rlm_end(data["session_id"])


def test_rlm_projects_add_rejects_ambiguous_main(tmp_path, monkeypatch):
    """Codex Medium: rlm_projects(add) must fail-fast on multiple MAIN without cf."""
    from rlm_tools_bsl import projects as projects_mod
    from rlm_tools_bsl.projects import ProjectRegistry
    from rlm_tools_bsl.server import _rlm_projects

    src = tmp_path / "src"
    _write(src / "main1" / "Configuration.xml", _CF_MAIN_XML)
    _write(src / "main2" / "Configuration.xml", _CF_MAIN_XML)

    reg_path = tmp_path / "projects.json"
    reg = ProjectRegistry(reg_path)
    monkeypatch.setattr(projects_mod, "_registry", reg)

    try:
        result = _rlm_projects(action="add", name="ambiguous", path=str(src), password="pw")
        data = json.loads(result)
        assert "error" in data
        assert "main_candidates" in data
        # project must NOT have been registered
        assert reg.list_projects() == []
    finally:
        monkeypatch.setattr(projects_mod, "_registry", None)


def test_rlm_projects_add_accepts_container_and_stores_raw_path(tmp_path, monkeypatch):
    """Fail-fast must NOT trigger for a valid container — raw path is stored as given."""
    from rlm_tools_bsl import projects as projects_mod
    from rlm_tools_bsl.projects import ProjectRegistry
    from rlm_tools_bsl.server import _rlm_projects

    src = _make_src_proj(tmp_path)

    reg_path = tmp_path / "projects.json"
    reg = ProjectRegistry(reg_path)
    monkeypatch.setattr(projects_mod, "_registry", reg)

    try:
        result = _rlm_projects(action="add", name="good", path=str(src), password="pw")
        data = json.loads(result)
        assert "added" in data
        # Registry preserves the user-supplied (container) path, not the cf-normalized one
        stored = reg.list_projects()
        assert len(stored) == 1
        assert Path(stored[0]["path"]) == Path(str(src))
    finally:
        monkeypatch.setattr(projects_mod, "_registry", None)


def test_rlm_start_project_hint_respects_normalized_path(tmp_path, monkeypatch):
    """Round-2 #1: registry has src/cf, user passes src → no false hint."""
    from rlm_tools_bsl import projects as projects_mod
    from rlm_tools_bsl.projects import ProjectRegistry
    from rlm_tools_bsl.server import _rlm_start, _rlm_end

    src = _make_src_proj(tmp_path)
    cf = src / "cf"

    # Isolated registry
    reg_path = tmp_path / "projects.json"
    reg = ProjectRegistry(reg_path)
    reg.add("test-src", str(cf), password="pw")

    monkeypatch.setattr(projects_mod, "_registry", reg)

    try:
        # User passes container, registry has cf-root → should NOT hint
        result = _rlm_start(path=str(src), query="test")
        data = json.loads(result)
        assert "project_hint" not in data, f"unexpected hint: {data.get('project_hint')}"
        _rlm_end(data["session_id"])
    finally:
        monkeypatch.setattr(projects_mod, "_registry", None)
