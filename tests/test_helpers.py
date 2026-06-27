import os
import tempfile

import pytest

from rlm_tools_bsl.helpers import make_helpers
from rlm_tools_bsl.regex_safety import has_catastrophic_nesting


def test_read_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "hello.txt")
        with open(test_file, "w") as f:
            f.write("hello world")

        helpers, _ = make_helpers(tmpdir)
        content = helpers["read_file"]("hello.txt")
        assert content == "hello world"


def test_read_file_blocks_path_traversal():
    with tempfile.TemporaryDirectory() as tmpdir:
        helpers, _ = make_helpers(tmpdir)
        try:
            helpers["read_file"]("../../etc/passwd")
            assert False, "Should have raised"
        except PermissionError:
            pass


def test_grep():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "code.py")
        with open(test_file, "w") as f:
            f.write("def hello():\n    pass\ndef world():\n    pass\n")

        helpers, _ = make_helpers(tmpdir)
        results = helpers["grep"]("def.*hello")
        assert len(results) > 0
        assert "hello" in results[0]["text"]


def test_glob_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "a.py"), "w").close()
        open(os.path.join(tmpdir, "b.py"), "w").close()
        open(os.path.join(tmpdir, "c.txt"), "w").close()

        helpers, _ = make_helpers(tmpdir)
        py_files = helpers["glob_files"]("**/*.py")
        assert len(py_files) == 2


def test_glob_files_dir_pattern_hint():
    with tempfile.TemporaryDirectory() as tmpdir:
        subdir = os.path.join(tmpdir, "MyModule")
        os.makedirs(subdir)
        open(os.path.join(subdir, "Module.bsl"), "w").close()

        helpers, _ = make_helpers(tmpdir)
        # Pattern matches a directory, not files — should return hint
        result = helpers["glob_files"]("My*")
        assert len(result) == 1
        assert result[0].startswith("[hint:")
        assert "1 directories" in result[0]
        assert "My*" in result[0]


def test_tree():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "src"))
        open(os.path.join(tmpdir, "src", "main.py"), "w").close()

        helpers, _ = make_helpers(tmpdir)
        output = helpers["tree"]()
        assert "src" in output
        assert "main.py" in output


def test_read_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "a.txt"), "w") as f:
            f.write("content a")
        with open(os.path.join(tmpdir, "b.txt"), "w") as f:
            f.write("content b")

        helpers, _ = make_helpers(tmpdir)
        result = helpers["read_files"](["a.txt", "b.txt"])
        assert result["a.txt"] == "content a"
        assert result["b.txt"] == "content b"


def test_read_files_handles_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "exists.txt"), "w") as f:
            f.write("hello")

        helpers, _ = make_helpers(tmpdir)
        result = helpers["read_files"](["exists.txt", "missing.txt"])
        assert result["exists.txt"] == "hello"
        assert "[error:" in result["missing.txt"]


def test_read_file_cache():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "cached.txt")
        with open(test_file, "w") as f:
            f.write("original")

        helpers, _ = make_helpers(tmpdir)
        first = helpers["read_file"]("cached.txt")
        assert first == "original"

        with open(test_file, "w") as f:
            f.write("modified")

        second = helpers["read_file"]("cached.txt")
        assert second == "original"  # cached, not re-read


def test_grep_summary():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "code.py"), "w") as f:
            f.write("def hello():\n    pass\ndef world():\n    pass\n")

        helpers, _ = make_helpers(tmpdir)
        output = helpers["grep_summary"]("def")
        assert "2 matches in 1 files:" in output
        assert "code.py" in output
        assert "L1:" in output
        assert "L3:" in output


def test_grep_summary_no_matches():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "code.py"), "w") as f:
            f.write("hello world\n")

        helpers, _ = make_helpers(tmpdir)
        output = helpers["grep_summary"]("zzz_nonexistent")
        assert output == "No matches found."


def test_grep_read():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "reducer.swift"), "w") as f:
            f.write("struct MyReducer: Reducer {\n    var body: some ReducerOf<Self> {\n    }\n}\n")
        with open(os.path.join(tmpdir, "model.swift"), "w") as f:
            f.write("struct Model {\n    var name: String\n}\n")

        helpers, _ = make_helpers(tmpdir)
        result = helpers["grep_read"]("Reducer")
        assert "reducer.swift" in result["matches"]
        assert "model.swift" not in result["matches"]
        assert "struct MyReducer" in result["files"]["reducer.swift"]
        assert "matches in 1 files" in result["summary"]


def test_grep_read_with_context():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "code.py"), "w") as f:
            f.write("line1\nline2\ntarget\nline4\nline5\n")

        helpers, _ = make_helpers(tmpdir)
        result = helpers["grep_read"]("target", context_lines=1)
        content = result["files"]["code.py"]
        assert "L2:" in content
        assert "L3:" in content
        assert "L4:" in content
        assert "L1:" not in content


def test_grep_read_max_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(5):
            with open(os.path.join(tmpdir, f"file{i}.py"), "w") as f:
                f.write(f"def func{i}():\n    pass\n")

        helpers, _ = make_helpers(tmpdir)
        result = helpers["grep_read"]("def", max_files=2)
        assert len(result["files"]) == 2
        assert "more" in result["summary"]


def test_find_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "CommonModules", "MyModule", "Ext"))
        with open(os.path.join(tmpdir, "CommonModules", "MyModule", "Ext", "Module.bsl"), "w") as f:
            f.write("// code")
        with open(os.path.join(tmpdir, "script.py"), "w") as f:
            f.write("pass")

        helpers, _ = make_helpers(tmpdir)
        results = helpers["find_files"]("Module.bsl")
        assert len(results) >= 1
        assert any("Module.bsl" in r for r in results)

        results2 = helpers["find_files"]("script")
        assert len(results2) >= 1

        results3 = helpers["find_files"]("nonexistent_xyz")
        assert len(results3) == 0


def test_find_files_case_insensitive():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "MyFile.txt"), "w") as f:
            f.write("test")

        helpers, _ = make_helpers(tmpdir)
        results = helpers["find_files"]("myfile")
        assert len(results) == 1


class _StaleIndexReader:
    """Стаб индекса для Finding #4: честный zero-hit (файла нет в индексе)."""

    def find_files_indexed(self, name, limit=100):
        return []


def test_find_files_fallback_on_stale_index():
    """Finding #4 (v1.26.0): индекс вернул [] (zero-hit), но файл есть на диске →
    find_files уходит в FS-fallback (раньше `if indexed is not None:` глотал []
    и возвращал пусто)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "НовыйФайл.bsl"), "w", encoding="utf-8") as f:
            f.write("// code")

        helpers, _ = make_helpers(tmpdir, idx_reader=_StaleIndexReader())
        results = helpers["find_files"]("НовыйФайл")
        assert len(results) >= 1
        assert any("НовыйФайл" in r for r in results)


@pytest.mark.xfail(
    strict=False,
    reason="FS-кэш find_files строится раз за сессию; файл, добавленный ПОСЛЕ "
    "прогрева кэша, в той же сессии не виден — отдельный follow-up (Finding #4 граница)",
)
def test_find_files_mid_session_cache_boundary():
    """Граница mid-session: файл, добавленный ПОСЛЕ прогрева FS-кэша, не виден
    в той же сессии. Закреплено как xfail (не passing-assert текущего ограничения),
    чтобы не мешать будущему улучшению (детект stale + merge)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "ПервыйФайл.bsl"), "w", encoding="utf-8") as f:
            f.write("// code")

        helpers, _ = make_helpers(tmpdir, idx_reader=_StaleIndexReader())
        # Прогрев FS-кэша через первый zero-hit
        assert helpers["find_files"]("ПервыйФайл")
        # Файл добавлен ПОСЛЕ прогрева кэша
        with open(os.path.join(tmpdir, "ВторойФайл.bsl"), "w", encoding="utf-8") as f:
            f.write("// code")
        assert helpers["find_files"]("ВторойФайл")


# --- Finding #2 (v1.26.0): regex catastrophic-backtracking guard ---


def test_regex_guard_detects_nested():
    """Неограниченная группа с неограниченным внутренним квантором → True."""
    assert has_catastrophic_nesting(r"(a+)+b")
    assert has_catastrophic_nesting(r"(a*)*")
    assert has_catastrophic_nesting(r"(\d+)+$")
    assert has_catastrophic_nesting(r"((ab)+)+")
    # Перекрытие классов внутри группы — реальный ReDoS, ожидаемо True (round 25).
    assert has_catastrophic_nesting(r"(\w+\s*)+")
    assert has_catastrophic_nesting(r"(\d+\D*)+")
    # Неограниченный квантор, спрятанный за bounded-квантором подгруппы: внешняя группа
    # всё равно неограниченно-квантифицирована, а её тело транзитивно содержит a+ →
    # реальный ReDoS (codex). Bounded {2} НЕ должен блокировать распространение факта.
    assert has_catastrophic_nesting(r"((a+){2})+b")
    assert has_catastrophic_nesting(r"((a+)?)*")


def test_regex_guard_allows_bounded():
    """Bounded inner кванторы линейны → НЕ блокировать (анти-регрессия, round 23)."""
    assert not has_catastrophic_nesting(r"(\d{4})+")
    assert not has_catastrophic_nesting(r"(\w{2,4})+")
    assert not has_catastrophic_nesting(r"(\d{3}-?)+")
    # Fully-bounded nesting stays linear → must NOT be blocked (no unbounded quantifier
    # anywhere, even transitively).
    assert not has_catastrophic_nesting(r"((\d{4}){2})+")


def test_regex_guard_allows_common():
    """Обычные паттерны не блокируются (нет вложенного неограниченного квантора)."""
    for pat in (r"\w+", r"[А-Яа-я]+", r"Процедура\s+\w+", r".*Обработка.*", r"(abc)+", r"(a+)?b"):
        assert not has_catastrophic_nesting(pat)


def test_regex_guard_escaping_and_classes():
    """Экранирование и классы символов не образуют группу/квантор;
    non-capturing и lookaround блокируются наравне с capturing."""
    assert not has_catastrophic_nesting(r"\(a+\)\+")  # экранированные скобки — не группа
    assert not has_catastrophic_nesting(r"[(+)]a+")  # ( + ) внутри класса — не группа
    assert has_catastrophic_nesting(r"(?:a+)+")  # non-capturing — блок
    assert has_catastrophic_nesting(r"(?=a+)+")  # lookahead — блок


def test_grep_rejects_catastrophic():
    """generic grep отклоняет catastrophic-паттерны ValueError'ом, без зависания."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "f.txt"), "w") as f:
            f.write("benign content\n")
        helpers, _ = make_helpers(tmpdir)
        for pat in (r"(a+)+b", r"(a*)*", r"(\d+)+$", r"((ab)+)+"):
            with pytest.raises(ValueError):
                helpers["grep"](pat, "f.txt")


def test_grep_allows_common_patterns():
    """Обычные паттерны работают как раньше."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "f.txt"), "w", encoding="utf-8") as f:
            f.write("Процедура Обработка\nabc123\n")
        helpers, _ = make_helpers(tmpdir)
        for pat in (r"\w+", r"[А-Яа-я]+", r"Процедура\s+\w+", r".*Обработка.*"):
            helpers["grep"](pat, "f.txt")  # не должно бросать


def test_read_file_utf8_sig():
    """Test that utf-8-sig BOM is handled correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "bom.txt")
        with open(test_file, "wb") as f:
            f.write(b"\xef\xbb\xbfhello with BOM")

        helpers, _ = make_helpers(tmpdir)
        content = helpers["read_file"]("bom.txt")
        assert content == "hello with BOM"
        assert not content.startswith("\ufeff")


def test_cache_invalidation_on_rename():
    """Cache must invalidate when files are renamed even if count stays the same."""
    from rlm_tools_bsl.cache import load_index, save_index
    from rlm_tools_bsl.format_detector import BslFileInfo

    with tempfile.TemporaryDirectory() as tmpdir:
        base = tmpdir

        def make_entry(path):
            return (
                path,
                BslFileInfo(
                    relative_path=path,
                    category="CommonModules",
                    object_name="Test",
                    module_type="Module",
                    form_name=None,
                    command_name=None,
                    is_form_module=False,
                ),
            )

        original = [make_entry("a.bsl"), make_entry("b.bsl")]
        save_index(base, bsl_count=2, entries=original)

        # Same count, same paths -> should load
        result = load_index(base, bsl_count=2, bsl_paths=["a.bsl", "b.bsl"])
        assert result is not None
        assert len(result) == 2

        # Same count, different paths -> should invalidate
        result2 = load_index(base, bsl_count=2, bsl_paths=["a.bsl", "c.bsl"])
        assert result2 is None

        # Different count -> should still invalidate
        result3 = load_index(base, bsl_count=3, bsl_paths=["a.bsl", "b.bsl", "c.bsl"])
        assert result3 is None


# ---------------------------------------------------------------------------
# glob_files fallback logging tests
# ---------------------------------------------------------------------------


def test_glob_files_indexed_no_fallback_log(caplog, tmp_path):
    """Supported pattern served from index should NOT log FS fallback."""
    from unittest.mock import MagicMock

    idx = MagicMock()
    idx.glob_files.return_value = ["a.bsl", "b.bsl"]

    helpers, _ = make_helpers(str(tmp_path), idx_reader=idx)
    with caplog.at_level("INFO", logger="rlm_tools_bsl.helpers"):
        result = helpers["glob_files"]("**/*.bsl")

    assert len(result) == 2
    assert not any("FS fallback" in msg for msg in caplog.messages)


def test_glob_files_unsupported_logs_fallback(caplog, tmp_path):
    """Unsupported pattern with index present should log reason=unsupported."""
    (tmp_path / "a.bsl").write_text("// code", encoding="utf-8")

    from unittest.mock import MagicMock

    idx = MagicMock()
    idx.glob_files.return_value = None  # unsupported pattern

    helpers, _ = make_helpers(str(tmp_path), idx_reader=idx)
    with caplog.at_level("INFO", logger="rlm_tools_bsl.helpers"):
        helpers["glob_files"]("**/Dir*/*.xml")

    fallback_msgs = [m for m in caplog.messages if "FS fallback" in m]
    assert len(fallback_msgs) == 1
    assert "reason=unsupported" in fallback_msgs[0]


def test_glob_files_no_index_logs_fallback(caplog, tmp_path):
    """Without index, should log reason=no_index."""
    (tmp_path / "a.bsl").write_text("// code", encoding="utf-8")

    helpers, _ = make_helpers(str(tmp_path), idx_reader=None)
    with caplog.at_level("INFO", logger="rlm_tools_bsl.helpers"):
        helpers["glob_files"]("**/*.bsl")

    fallback_msgs = [m for m in caplog.messages if "FS fallback" in m]
    assert len(fallback_msgs) == 1
    assert "reason=no_index" in fallback_msgs[0]


def test_glob_files_index_error_logs_fallback(caplog, tmp_path):
    """Index error should log reason=index_error and fall back to FS."""
    (tmp_path / "a.bsl").write_text("// code", encoding="utf-8")

    from unittest.mock import MagicMock

    idx = MagicMock()
    idx.glob_files.side_effect = RuntimeError("db locked")

    helpers, _ = make_helpers(str(tmp_path), idx_reader=idx)
    with caplog.at_level("INFO", logger="rlm_tools_bsl.helpers"):
        result = helpers["glob_files"]("**/*.bsl")

    fallback_msgs = [m for m in caplog.messages if "FS fallback" in m]
    assert len(fallback_msgs) == 1
    assert "reason=index_error" in fallback_msgs[0]
    # FS fallback should still return results
    assert len(result) == 1
