"""v1.23.0 — server-side efficiency nudges (session-cumulative, throttled).

Nudges live in the rlm_execute response metadata (never the helper return / stdout)
and target the call-leak classes from the A/B logs: non-batched reads, re-resolves.
"""

from __future__ import annotations

import os
import tempfile

from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.sandbox import Sandbox


def _txt_sandbox(tmpdir, names=("a", "b", "c")):
    for n in names:
        with open(os.path.join(tmpdir, f"{n}.txt"), "w", encoding="utf-8") as f:
            f.write("CONTENT")
    return Sandbox(base_path=tmpdir)


def _bsl_sandbox(tmpdir):
    obj = os.path.join(tmpdir, "Documents", "Док", "Ext")
    os.makedirs(obj)
    with open(os.path.join(obj, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write("Процедура П() Экспорт\nКонецПроцедуры\n")
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    return Sandbox(base_path=tmpdir, format_info=detect_format(tmpdir))


def test_read_file_triggers_read_files_nudge():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("read_file('a.txt'); read_file('b.txt'); read_file('c.txt')")
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "read_files" in ids
        h = next(h for h in res.efficiency_hints if h["id"] == "read_files")
        assert h["helper"] == "read_file"
        assert h["count"] >= 3
        assert "read_files([" in h["message"]


def test_batched_read_files_no_nudge():
    """Using the aggregate form (read_files) once → nothing to nudge."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("d = read_files(['a.txt','b.txt','c.txt'])")
        assert not res.efficiency_hints


def test_repeated_find_module_triggers_reuse_var():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        res = sb.execute("find_module('Док')")  # same arg fingerprint
        ids = {h["id"] for h in (res.efficiency_hints or [])}
        assert "reuse_var" in ids
        h = next(h for h in res.efficiency_hints if h["id"] == "reuse_var")
        assert h["helper"] == "find_module"


def test_different_find_module_args_do_not_trigger_reuse():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        res = sb.execute("find_module('Другое')")  # different fingerprint
        assert not any(h["id"] == "reuse_var" for h in (res.efficiency_hints or []))


def test_nudge_throttled_once_per_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _bsl_sandbox(tmpdir)
        sb.execute("find_module('Док')")
        r2 = sb.execute("find_module('Док')")
        r3 = sb.execute("find_module('Док')")
        assert any(h["id"] == "reuse_var" for h in (r2.efficiency_hints or []))
        assert not any(h["id"] == "reuse_var" for h in (r3.efficiency_hints or []))


def test_aggregator_is_instance_local():
    """Two sandboxes never share nudge state (no module singleton leak across sessions)."""
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        sb1 = _txt_sandbox(t1)
        sb2 = _txt_sandbox(t2)
        sb1.execute("read_file('a.txt'); read_file('b.txt'); read_file('c.txt')")
        # sb2 is fresh — one read, no nudge
        res2 = sb2.execute("read_file('a.txt')")
        assert not res2.efficiency_hints


def test_hints_in_metadata_not_stdout_and_return_unchanged():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = _txt_sandbox(tmpdir)
        res = sb.execute("x = read_file('a.txt'); read_file('b.txt'); read_file('c.txt'); print(x)")
        assert res.efficiency_hints  # present in metadata
        # NOT leaked into stdout
        assert "read_files([" not in res.stdout
        assert "HINT" not in res.stdout
        # helper return value unchanged (x is the file content)
        assert "CONTENT" in res.stdout
