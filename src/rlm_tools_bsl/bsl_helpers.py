from __future__ import annotations
import collections
import concurrent.futures
import json
import logging
import os
import re
import threading
import time as _time_mod
from dataclasses import replace
from pathlib import Path
from rlm_tools_bsl.format_detector import parse_bsl_path, BslFileInfo, FormatInfo
from rlm_tools_bsl.bsl_knowledge import BSL_PATTERNS, _merge_proc_continuations
from rlm_tools_bsl.cache import load_index, save_index
from rlm_tools_bsl.helpers import _SKIP_DIRS as _GENERIC_SKIP_DIRS

logger = logging.getLogger(__name__)
from rlm_tools_bsl.bsl_xml_parsers import (
    _normalize_category,
    parse_metadata_xml,
    parse_event_subscription_xml,
    parse_scheduled_job_xml,
    parse_enum_xml,
    parse_functional_option_xml,
    parse_rights_xml,
)

# Regex metacharacters. A pattern with none of these is a plain literal, so a
# ``git grep -F`` over it is identical to a Python ``re.search`` — that lets
# safe_grep route literal patterns through the (much faster) git backend while
# keeping real regexes on Python ``re``.
_RE_METACHARS = frozenset(r"\^$.|?*+()[]{}")


def _is_literal_pattern(pattern: str) -> bool:
    """True when *pattern* contains no regex metacharacters (treat as literal)."""
    return not any(c in _RE_METACHARS for c in pattern)


class LazyList:
    """Thread-safe lazy-init list with double-check locking."""

    __slots__ = ("data", "_built", "_lock")

    def __init__(self):
        self.data: list = []
        self._built = False
        self._lock = threading.Lock()

    def ensure(self, builder):
        if self._built:
            return self.data
        with self._lock:
            if not self._built:
                self.data.extend(builder())
                self._built = True
        return self.data


class LazyDict:
    """Thread-safe per-key lazy cache with double-check locking."""

    __slots__ = ("data", "_lock")

    def __init__(self):
        self.data: dict = {}
        self._lock = threading.Lock()

    def get_or_set(self, key, builder):
        if key in self.data:
            return self.data[key]
        with self._lock:
            if key not in self.data:
                self.data[key] = builder()
        return self.data[key]


# --- Static helper-metadata snapshot for `rlm_help` -------------------------
# `make_bsl_helpers` registers every helper into its closure-local `_registry`
# even before any helper function is called: registration only writes
# {sig, cat, kw, recipe} via `_reg(...)`. We exploit that to build a static
# snapshot of helper metadata without an active sandbox or filesystem — the
# stub callbacks below are wired only because `make_bsl_helpers` requires
# them; their behaviour is irrelevant because no helper body is executed.

_HELPER_METADATA_SNAPSHOT: dict[str, dict] | None = None
_HELPER_METADATA_SNAPSHOT_LOCK = threading.Lock()


def build_helper_metadata_snapshot() -> dict[str, dict]:
    """Return a frozen ``{name: {sig, cat, kw, recipe}}`` map of every helper.

    Module-level cache; first call pays the registration cost (no I/O), every
    subsequent call returns the same dict instance. Used by the ``rlm_help``
    MCP tool to answer ``category=`` / ``helpers=`` / menu queries without
    holding an open session.
    """
    global _HELPER_METADATA_SNAPSHOT
    if _HELPER_METADATA_SNAPSHOT is not None:
        return _HELPER_METADATA_SNAPSHOT
    with _HELPER_METADATA_SNAPSHOT_LOCK:
        if _HELPER_METADATA_SNAPSHOT is not None:
            return _HELPER_METADATA_SNAPSHOT

        def _stub_resolve_safe(p):
            return Path(p)

        def _stub_read(_p):
            return ""

        def _stub_grep(_pat, _p="."):
            return []

        def _stub_glob(_pat):
            return []

        helpers = make_bsl_helpers(
            base_path=".",
            resolve_safe=_stub_resolve_safe,
            read_file_fn=_stub_read,
            grep_fn=_stub_grep,
            glob_files_fn=_stub_glob,
            # Force git_search into the snapshot regardless of the server's cwd /
            # whether git is reachable, so `rlm_help git_search` is always
            # documented. Live sessions gate it via "auto" (see make_bsl_helpers).
            register_git_search="force",
        )
        registry = helpers.get("_registry") or {}
        snapshot: dict[str, dict] = {}
        for name, entry in registry.items():
            snapshot[name] = {
                "sig": entry.get("sig", ""),
                "cat": entry.get("cat", ""),
                "kw": list(entry.get("kw") or []),
                "recipe": entry.get("recipe", ""),
            }
        _HELPER_METADATA_SNAPSHOT = snapshot
        return snapshot


def make_bsl_helpers(
    base_path: str,
    resolve_safe,  # callable: str -> pathlib.Path
    read_file_fn,  # callable: str -> str
    grep_fn,  # callable: (pattern, path) -> list[dict]
    glob_files_fn,  # callable: (pattern) -> list[str]
    format_info: FormatInfo | None = None,
    idx_reader=None,  # optional IndexReader for SQLite index acceleration
    idx_zero_callers_authoritative: bool = False,
    extension_paths: list[str] | None = None,
    register_git_search: str = "auto",
) -> dict:
    """Creates BSL helper functions for sandbox namespace.
    Internal _bsl_index is built lazily on first find_module() call.
    If idx_reader is provided, helpers use it as a fast path with fallback.

    ``extension_paths`` — absolute paths to nearby extension roots (only when
    sandbox base is a MAIN config). When non-empty, the lazy index pass also
    scans BSL + metadata XML/MDO under each extension root so that find_module,
    find_attributes, parse_object_xml, find_predefined and search() see the
    extension objects. The generic sandbox resolver (helpers.make_helpers) is
    NOT touched — extension files stay invisible to read_file/grep/glob_files.

    ``register_git_search`` controls the opt-in full-text ``git_search`` helper:
    ``"auto"`` (live sessions) registers it only when *base_path* is under a git
    work-tree and ``git`` is reachable; ``"force"`` always registers it (used by
    the rlm_help doc snapshot, independent of cwd/git); ``"never"`` never does.
    """

    _base_path_resolved = Path(base_path).resolve()
    _ext_paths_raw: list[str] = list(extension_paths or [])
    _ext_roots_resolved: list[Path] = []
    for ext in _ext_paths_raw:
        try:
            _ext_roots_resolved.append(Path(ext).resolve())
        except OSError:
            continue

    # Caches/structures filled during _ensure_index extension pass.
    _extension_paths_set: set[str] = set()
    _extension_root_for: dict[str, str] = {}
    _extension_metadata_xml: list[tuple[str, str, str]] = []  # (category, object_name, rel_xml_to_base)
    _extension_synonyms: list[tuple[str, str, str, str]] = []  # (obj_name, category, prefixed_synonym, rel_to_base)

    # Small OrderedDict cache for files outside the sandbox base (extension reads).
    _ext_file_cache: "collections.OrderedDict[str, str]" = collections.OrderedDict()
    _ext_file_cache_lock = threading.Lock()
    _EXT_FILE_CACHE_MAX = 200

    # Per-session parsed-attribute / parsed-predefined caches for extensions.
    # Built lazily on first name-only find_attributes / find_predefined call —
    # subsequent calls filter the cache in memory instead of re-parsing XML
    # for every ext object. Critical for large extensions (~150+ objects) where
    # parsing all metadata XMLs takes 5-15s on cold cache.
    _ext_attrs_cache: dict[tuple[str, str], list[dict]] = {}
    _ext_attrs_cache_built: list[bool] = [False]
    _ext_attrs_cache_lock = threading.Lock()
    _ext_predefined_cache: dict[tuple[str, str], list[dict]] = {}
    _ext_predefined_cache_built: list[bool] = [False]
    _ext_predefined_cache_lock = threading.Lock()

    # Lazy, per-session git-availability for the full-text search backend.
    # Cheap ``.git``-ancestor pre-check, confirmed by ``_git_available`` (one
    # subprocess per session, then cached). Gates git_search registration and
    # routes safe_grep's literal patterns through the git backend.
    _git_search_state: dict = {"checked": False, "available": False}
    _git_search_lock = threading.Lock()

    def _git_search_available() -> bool:
        if _git_search_state["checked"]:
            return _git_search_state["available"]
        with _git_search_lock:
            if _git_search_state["checked"]:
                return _git_search_state["available"]
            avail = False
            try:
                has_git = False
                for cand in (_base_path_resolved, *_base_path_resolved.parents):
                    if (cand / ".git").exists():  # dir (normal) or file (worktree)
                        has_git = True
                        break
                if has_git:
                    from rlm_tools_bsl.bsl_index import _git_available

                    avail = bool(_git_available(base_path))
            except Exception:
                avail = False
            _git_search_state["available"] = avail
            _git_search_state["checked"] = True
            return avail

    def _ext_resolve_safe(path: str) -> Path:
        """Multi-root path resolver: accept any path resolving under base OR any
        configured extension root. Raises PermissionError when outside all roots.

        Generic sandbox-base-only invariants in ``read_file``/``grep``/``glob_files``
        are NOT affected — this resolver is internal to BSL-helpers that already
        receive `../`-relative paths from ``_index_state``.
        """
        candidate = (_base_path_resolved / path).resolve()
        # Cheap path: inside base.
        try:
            candidate.relative_to(_base_path_resolved)
            return candidate
        except ValueError:
            pass
        # Try each extension root.
        for ext_root in _ext_roots_resolved:
            try:
                candidate.relative_to(ext_root)
                return candidate
            except ValueError:
                continue
        raise PermissionError(f"Access denied: path '{path}' escapes sandbox and extension roots")

    def _ext_read_file(path: str) -> str:
        """Reader that delegates to the sandbox cache for base files and reads
        extension-root files directly (with a small OrderedDict cache).
        """
        resolved = _ext_resolve_safe(path)
        try:
            resolved.relative_to(_base_path_resolved)
            in_base = True
        except ValueError:
            in_base = False

        if in_base:
            # Delegate to the sandbox cache via the wrapped read_file_fn.
            return read_file_fn(path)

        key = str(resolved)
        with _ext_file_cache_lock:
            if key in _ext_file_cache:
                _ext_file_cache.move_to_end(key)
                return _ext_file_cache[key]
        content = resolved.read_text(encoding="utf-8-sig", errors="replace")
        with _ext_file_cache_lock:
            _ext_file_cache[key] = content
            if len(_ext_file_cache) > _EXT_FILE_CACHE_MAX:
                _ext_file_cache.popitem(last=False)
        return content

    # Mutable closure state for lazy index
    _index_state: list = []  # list of tuples (relative_path, BslFileInfo)
    _index_built: list[bool] = [False]
    _index_lock = threading.Lock()

    def _load_main_into_index_state() -> None:
        """Load main config modules into _index_state (idx_reader or glob+cache)."""
        # Fast path: load from SQLite index (instant, <1s)
        if idx_reader is not None:
            try:
                rows = idx_reader.get_all_modules()
                for r in rows:
                    info = BslFileInfo(
                        relative_path=r["rel_path"],
                        category=r["category"],
                        object_name=r["object_name"],
                        module_type=r["module_type"],
                        form_name=r["form_name"],
                        command_name=None,
                        is_form_module=bool(r["form_name"]),
                    )
                    _index_state.append((r["rel_path"], info))
                return
            except Exception:
                pass  # fallback to glob

        # Fallback: glob + disk cache
        all_bsl = glob_files_fn("**/*.bsl")
        bsl_count = len(all_bsl)

        cached = load_index(base_path, bsl_count, bsl_paths=all_bsl)
        if cached is not None:
            _index_state.extend(cached)
        else:
            for file_path in all_bsl:
                info = parse_bsl_path(file_path, base_path)
                _index_state.append((info.relative_path, info))
            save_index(base_path, bsl_count, _index_state)

    def _load_extensions_into_index_state() -> None:
        """Scan each extension root for BSL + metadata XML/MDO and side-load
        into _index_state with paths relative to the main base.
        """
        if not _ext_roots_resolved:
            return

        # Lazy import — avoids a cycle since bsl_index imports from bsl_knowledge.
        try:
            from rlm_tools_bsl.bsl_index import _collect_object_synonyms, _iter_metadata_xml_files
        except Exception:  # pragma: no cover - defensive
            _iter_metadata_xml_files = None  # type: ignore[assignment]
            _collect_object_synonyms = None  # type: ignore[assignment]

        total_ext_files = 0
        for ext_root in _ext_roots_resolved:
            if not ext_root.is_dir():
                continue
            ext_root_str = str(ext_root)

            # --- BSL pass ---
            for dirpath, dirnames, filenames in os.walk(ext_root):
                dirnames[:] = [d for d in dirnames if d not in _GENERIC_SKIP_DIRS and not d.startswith(".")]
                for fname in filenames:
                    if not fname.lower().endswith(".bsl"):
                        continue
                    full = Path(dirpath) / fname
                    try:
                        rel = os.path.relpath(str(full), base_path).replace("\\", "/")
                    except ValueError:
                        continue
                    info_ext = parse_bsl_path(str(full), ext_root_str)
                    info_bound = replace(info_ext, relative_path=rel)
                    _index_state.append((rel, info_bound))
                    _extension_paths_set.add(rel)
                    _extension_root_for[rel] = ext_root_str
                    total_ext_files += 1

            # --- Metadata-XML pass: locators for all ext objects (incl. XML-only) ---
            if _iter_metadata_xml_files is not None:
                try:
                    locators = _iter_metadata_xml_files(ext_root_str)
                except Exception:
                    locators = []
                for cat, obj_name, rel_to_ext in locators:
                    try:
                        rel_to_base = os.path.relpath(str(ext_root / rel_to_ext), base_path).replace("\\", "/")
                    except ValueError:
                        continue
                    _extension_metadata_xml.append((cat, obj_name, rel_to_base))

            # --- Synonyms pass: parity with index for search_objects ---
            if _collect_object_synonyms is not None:
                try:
                    syn_rows = _collect_object_synonyms(ext_root_str)
                except Exception:
                    syn_rows = []
                for obj_name, cat, prefixed_synonym, rel_to_ext in syn_rows:
                    try:
                        rel_to_base = os.path.relpath(str(ext_root / rel_to_ext), base_path).replace("\\", "/")
                    except ValueError:
                        continue
                    _extension_synonyms.append((obj_name, cat, prefixed_synonym, rel_to_base))

        if total_ext_files > 5000:
            logger.warning(
                "extension pass scanned %d BSL files — consider RLM_EXTENSION_MAX_FILES env or check ext layout",
                total_ext_files,
            )

    def _ensure_index() -> None:
        if _index_built[0]:
            return
        with _index_lock:
            if _index_built[0]:
                return
            _load_main_into_index_state()
            _load_extensions_into_index_state()
            _index_built[0] = True

    # --- Auto-detect custom prefixes from object names ---
    _detected_prefixes: list[str] = []
    _prefixes_built: list[bool] = [False]
    _prefixes_lock = threading.Lock()

    def _ensure_prefixes() -> list[str]:
        if _prefixes_built[0]:
            return _detected_prefixes
        with _prefixes_lock:
            if _prefixes_built[0]:
                return _detected_prefixes
            _ensure_index()

            # Collect unique object names from index
            object_names: set[str] = set()
            for _, info in _index_state:
                if info.object_name:
                    object_names.add(info.object_name)

            # Custom objects start with a lowercase letter in 1C conventions.
            # Extract prefix: sequence of lowercase letters (+ optional _) before
            # the first uppercase letter.
            prefix_re = re.compile(r"^([a-zа-яё]+_?)")
            prefix_counts: dict[str, int] = {}
            for name in object_names:
                if not name or not name[0].islower():
                    continue
                m = prefix_re.match(name)
                if m:
                    prefix = m.group(1)
                    # Normalize: strip trailing _ for counting, keep in result
                    key = prefix.rstrip("_").lower()
                    if len(key) >= 2:
                        prefix_counts[key] = prefix_counts.get(key, 0) + 1

            # For extensions, lower threshold to 1 (fewer custom objects expected)
            config_role = None
            if idx_reader is not None:
                try:
                    config_role = idx_reader.get_statistics().get("config_role")
                except Exception:
                    pass
            min_count = 1 if config_role == "extension" else 3

            frequent = sorted(
                ((k, v) for k, v in prefix_counts.items() if v >= min_count),
                key=lambda x: -x[1],
            )
            _detected_prefixes.clear()
            _detected_prefixes.extend(k for k, _ in frequent)

            _prefixes_built[0] = True
            return _detected_prefixes

    # --- Strip 1C metadata type prefixes from object names ---
    # Models often pass "Документ.РеализацияТоваровУслуг" instead of "РеализацияТоваровУслуг"
    _META_TYPE_PREFIXES = (
        "Документ.",
        "Справочник.",
        "Перечисление.",
        "РегистрСведений.",
        "РегистрНакопления.",
        "РегистрБухгалтерии.",
        "РегистрРасчета.",
        "Отчет.",
        "Обработка.",
        "ПланОбмена.",
        "ПланСчетов.",
        "ПланВидовХарактеристик.",
        "ПланВидовРасчета.",
        "БизнесПроцесс.",
        "Задача.",
        "Константа.",
        "ПодпискаНаСобытие.",
        "РегламентноеЗадание.",
        "Document.",
        "Catalog.",
        "Enum.",
        "InformationRegister.",
        "AccumulationRegister.",
        "AccountingRegister.",
        "CalculationRegister.",
        "Report.",
        "DataProcessor.",
        "ExchangePlan.",
        "ChartOfAccounts.",
        "ChartOfCharacteristicTypes.",
        "ChartOfCalculationTypes.",
        "BusinessProcess.",
        "Task.",
        "Constant.",
        "DocumentObject.",
        "CatalogObject.",
        "DocumentRef.",
        "CatalogRef.",
        "ДокументОбъект.",
        "СправочникОбъект.",
        "ДокументСсылка.",
        "СправочникСсылка.",
        "ОбщаяФорма.",
        "CommonForm.",
    )

    def _strip_meta_prefix(name: str) -> str:
        """Strip 1C metadata type prefix if present: 'Документ.X' -> 'X'."""
        for prefix in _META_TYPE_PREFIXES:
            if name.startswith(prefix):
                return name[len(prefix) :]
        return name

    def _info_to_dict(relative_path: str, info: BslFileInfo) -> dict:
        return {
            "path": relative_path,
            "category": info.category,
            "object_name": info.object_name,
            "module_type": info.module_type,
            "form_name": info.form_name,
        }

    # ── Helper registry ──────────────────────────────────────────
    _registry: dict[str, dict] = {}

    def _reg(name: str, fn, sig: str, cat: str, kw: list[str] | None = None, recipe: str = ""):
        """Register a helper: sig for strategy table, kw+recipe for help()."""
        _registry[name] = {
            "fn": fn,
            "sig": sig,
            "cat": cat,
            "kw": kw or [],
            "recipe": recipe,
        }

    def find_module(name: str) -> list[dict]:
        """Find BSL modules by name fragment (case-insensitive).

        Returns: list of dicts {path, category, object_name, module_type, form_name}."""
        name = _strip_meta_prefix(name)
        _ensure_index()
        name_lower = name.lower()
        results = []
        for relative_path, info in _index_state:
            matched = False
            if info.object_name and name_lower in info.object_name.lower():
                matched = True
            if not matched and name_lower in relative_path.lower():
                matched = True
            if matched:
                results.append(_info_to_dict(relative_path, info))
            if len(results) >= 50:
                break
        return results

    def find_by_type(meta_type: str, name: str = "") -> list[dict]:
        """Find BSL modules by metadata category, optionally filtered by object name.

        Accepts plural folder names (InformationRegisters), singular (InformationRegister),
        and Russian names (РегистрСведений).
        Categories: CommonModules, Documents, Catalogs, InformationRegisters,
        AccumulationRegisters, AccountingRegisters, CalculationRegisters,
        Reports, DataProcessors, Constants.

        Returns: list of dicts {path, category, object_name, module_type, form_name}."""
        name = _strip_meta_prefix(name)
        _ensure_index()
        meta_type_lower = _normalize_category(meta_type)
        name_lower = name.lower()
        results = []
        for relative_path, info in _index_state:
            if not info.category or info.category.lower() != meta_type_lower:
                continue
            if name_lower and (not info.object_name or name_lower not in info.object_name.lower()):
                continue
            results.append(_info_to_dict(relative_path, info))
            if len(results) >= 50:
                break
        return results

    _proc_lazy = LazyDict()
    _prefilter_lazy = LazyDict()

    def _parse_procedures(path: str) -> list[dict]:
        """Parse BSL file — internal, result gets cached by LazyDict.

        Handles multi-line procedure signatures (``Процедура X(a,\n  b)``) by
        merging continuation lines before matching ``BSL_PATTERNS['procedure_def']``.
        ``end_line`` is taken from the original line list.
        """
        content = _ext_read_file(path)
        lines = content.splitlines()
        merged_lines, line_map = _merge_proc_continuations(lines)
        total_orig = len(lines)
        total_merged = len(merged_lines)

        proc_def_re = re.compile(BSL_PATTERNS["procedure_def"], re.IGNORECASE)
        proc_end_re = re.compile(BSL_PATTERNS["procedure_end"], re.IGNORECASE)

        procedures: list[dict] = []
        m_idx = 0
        while m_idx < total_merged:
            merged = merged_lines[m_idx]
            m = proc_def_re.search(merged)
            if not m:
                m_idx += 1
                continue

            proc_type = m.group(1)
            proc_name = m.group(2)
            params = m.group(3).strip() if m.group(3) else ""
            is_export = m.group(4) is not None and m.group(4).strip() != ""
            line_number = line_map[m_idx]  # 1-based

            next_start = line_map[m_idx + 1] if m_idx + 1 < total_merged else total_orig + 1
            scan_from = next_start - 1

            end_line: int | None = None
            for orig_idx in range(scan_from, total_orig):
                if proc_end_re.search(lines[orig_idx]):
                    end_line = orig_idx + 1
                    break

            if end_line is None:
                procedures.append(
                    {
                        "name": proc_name,
                        "type": proc_type,
                        "line": line_number,
                        "is_export": is_export,
                        "end_line": total_orig,
                        "params": params,
                    }
                )
                break

            procedures.append(
                {
                    "name": proc_name,
                    "type": proc_type,
                    "line": line_number,
                    "is_export": is_export,
                    "end_line": end_line,
                    "params": params,
                }
            )

            new_m = m_idx + 1
            while new_m < total_merged and line_map[new_m] <= end_line:
                new_m += 1
            m_idx = new_m

        return procedures

    def _attach_overrides(result: list[dict], overrides_map: dict | None) -> None:
        """Mutate ``result`` in place: attach ``overridden_by`` from a
        case-insensitive (Cyrillic) ``{name -> [override_dicts]}`` map.
        """
        if not overrides_map:
            return
        ov_lower = {k.lower(): v for k, v in overrides_map.items()}
        for proc in result:
            method_overrides = ov_lower.get(proc["name"].lower())
            if method_overrides:
                proc["overridden_by"] = [
                    {
                        "annotation": ov.get("annotation", ""),
                        "extension_name": ov.get("extension_name", ""),
                        "extension_method": ov.get("extension_method", ""),
                        "extension_root": ov.get("extension_root", ""),
                        "ext_module_path": ov.get("ext_module_path", ""),
                        "ext_line": ov.get("ext_line"),
                    }
                    for ov in method_overrides
                ]

    def extract_procedures(path: str) -> list[dict]:
        """Parse BSL file and return list of procedures/functions with metadata.
        Results are memoized per file path within the session.
        Uses SQLite index when available (instant), falls back to regex parsing.

        For indexed paths, also performs an opportunistic live-fill: if the
        live regex parser finds a procedure NOT present in the index (typically
        a multi-line signature that older indexes missed), it is appended to
        the result with the same shape, including ``overridden_by`` enrichment
        from ``idx_reader.get_overrides_for_path``. This makes the helper
        self-healing — multi-line procedures appear immediately, without
        requiring ``rlm-bsl-index index update``.

        Returns: list of dicts {name, type, line, end_line, is_export, params, overridden_by?}."""

        def _extract_with_index():
            overrides_map: dict | None = None
            if idx_reader is not None:
                try:
                    overrides_map = idx_reader.get_overrides_for_path(path)
                except Exception:
                    overrides_map = None

            result: list[dict] | None = None
            if idx_reader is not None:
                idx_result = idx_reader.get_methods_by_path(path)
                if idx_result is not None:
                    result = idx_result
                    _attach_overrides(result, overrides_map)

            if result is None:
                # No index — fall through to live parsing.
                live = _parse_procedures(path)
                _attach_overrides(live, overrides_map)
                return live

            # Opportunistic live-fill: add procedures missing from the index.
            try:
                live = _parse_procedures(path)
            except Exception:
                return result
            existing_names = {p["name"].lower() for p in result}
            additions: list[dict] = []
            for proc in live:
                if proc["name"].lower() in existing_names:
                    continue
                additions.append(proc)
            if additions:
                _attach_overrides(additions, overrides_map)
                result.extend(additions)
            return result

        return _proc_lazy.get_or_set(path, _extract_with_index)

    def find_exports(path: str) -> list[dict]:
        """Return only exported procedures/functions from a BSL file.

        Returns: list of dicts {name, type, line, end_line, is_export, params}."""
        return [p for p in extract_procedures(path) if p["is_export"]]

    def safe_grep(pattern: str, name_hint: str = "", max_files: int = 20) -> list[dict]:
        """Timeout-safe grep across BSL files, optionally scoped by module name hint.

        Contract is unchanged: returns ``[{file, line, text}]`` (no sentinel, no
        result cap — scope is bounded by *max_files* candidates). When the sources
        are under git **and** *pattern* is a plain literal, the non-extension
        (base) candidates are searched with a single ``git grep`` call instead of
        a thread-pool of per-file Python greps — the result is identical (literal
        == substring) but far cheaper. Real regexes stay on Python ``re`` (git
        ``-E`` is POSIX ERE, not equivalent to Python ``re``), and extension files
        always use the Python path (they live outside the sandbox base, which
        ``git -C base`` would not see).
        """
        _ensure_index()

        if name_hint:
            candidates = find_module(name_hint)
            paths = [c["path"] for c in candidates[:max_files]]
        else:
            paths = [relative_path for relative_path, _ in _index_state[:max_files]]

        if not paths:
            return []

        results: list[dict] = []
        py_paths: list[str] = list(paths)  # files still needing the Python path

        # Fast literal path via git grep over base (non-extension) candidates.
        if _is_literal_pattern(pattern) and _git_search_available():
            base_paths = [p for p in paths if p not in _extension_paths_set]
            if base_paths:
                from rlm_tools_bsl.bsl_index import _git_grep

                git_res = _git_grep(
                    base_path,
                    pattern,
                    literal_files=base_paths,
                    regex=False,
                    mode="lines",
                    max_results=10**9,  # no cap: scope already bounded by max_files
                    max_per_file=0,  # no per-file cap (parity with Python path)
                    include_truncation_sentinel=False,  # strict [{file,line,text}]
                )
                if git_res is not None:
                    results.extend(git_res)
                    base_set = set(base_paths)
                    py_paths = [p for p in paths if p not in base_set]

        compiled = re.compile(pattern)

        def _grep_one(path: str) -> list[dict]:
            # Base paths: delegate to generic grep (cached, sandbox-checked).
            # Extension paths: read via _ext_read_file (sandbox base-only grep
            # would raise PermissionError) and apply the same regex contract.
            if path in _extension_paths_set:
                try:
                    content = _ext_read_file(path)
                except Exception:
                    return []
                out: list[dict] = []
                for i, line in enumerate(content.splitlines(), 1):
                    if compiled.search(line):
                        out.append({"file": path, "line": i, "text": line.strip()})
                return out
            try:
                return grep_fn(pattern, path) or []
            except Exception:
                return []

        if len(py_paths) > 1:
            from concurrent.futures import ThreadPoolExecutor as _TP

            with _TP(max_workers=min(8, len(py_paths))) as pool:
                all_results = list(pool.map(_grep_one, py_paths))
            for batch in all_results:
                results.extend(batch)
        elif py_paths:
            results.extend(_grep_one(py_paths[0]))

        # Deterministic order: sort by (file, line)
        results.sort(key=lambda m: (m.get("file", ""), m.get("line", 0)))
        return results

    def git_search(
        pattern: str,
        path: str = "",
        file_types: str = "",
        regex: bool = False,
        ignore_case: bool = False,
        mode: str = "lines",
        max_results: int = 200,
    ) -> list[dict]:
        """Full-text search across ALL files under git (opt-in, only when the
        sources are a git work-tree).

        Unlike ``safe_grep`` (scoped to a known module / a bounded candidate set)
        this searches every tracked + untracked-not-ignored file — including raw
        ``.xml``/``.mdo`` (forms, rights, DCS, ConfigDumpInfo) and procedure
        bodies / string literals / query text that the name-based helpers and the
        SQLite index never see.

        Args:
            pattern: literal substring (default) or POSIX ERE when *regex* is True.
            path: optional subtree/file filter (e.g. ``"CommonModules"``).
            file_types: optional comma-separated extensions (e.g. ``"bsl,xml"``).
            regex: treat *pattern* as POSIX ERE. NOTE: on CRLF files a trailing
                CR sits before the line end, so the ``$`` anchor needs
                ``[[:space:]]*$`` (git matches bytes and its ERE does NOT read
                ``\\r`` as a carriage return — it is a literal ``r``).
            ignore_case: case-insensitive match.
            mode: ``"lines"`` → ``[{file, line, text}]``; ``"files"`` → ``[{file}]``
                (cheap overview — use first on common tokens, then drill down).
            max_results: cap; when hit, the last element is
                ``{"_truncated": True, "shown": max_results}``.

        Returns the hit list, or ``[{"error": ...}]`` if git grep failed / timed
        out / a filter was malformed (distinct from ``[]`` = nothing found).
        """
        from rlm_tools_bsl.bsl_index import _git_grep

        res = _git_grep(
            base_path,
            pattern,
            path=path,
            file_types=file_types,
            regex=regex,
            ignore_case=ignore_case,
            mode=mode,
            max_results=max_results,
            include_truncation_sentinel=True,
        )
        if res is None:
            return [{"error": "git grep failed or timed out"}]
        return res

    def read_procedure(
        path: str, proc_name: str, include_overrides: bool = False, numbered: bool = False
    ) -> str | None:
        """Extract a single procedure body from a BSL file by name.
        With include_overrides=True, appends extension override bodies if available."""
        procedures = extract_procedures(path)
        target = None
        for p in procedures:
            if p["name"].lower() == proc_name.lower():
                target = p
                break
        if target is None:
            return None

        content = _ext_read_file(path)
        lines = content.splitlines()

        start = target["line"] - 1  # convert to 0-based
        end = target["end_line"] if target["end_line"] is not None else len(lines)
        # end_line is 1-based and inclusive
        extracted = lines[start:end]
        body = "\n".join(extracted)

        if numbered:
            from rlm_tools_bsl._format import number_lines

            body = number_lines(body, start=target["line"])

        if not include_overrides:
            return body

        # Enrich with extension override bodies
        override_list = target.get("overridden_by")
        if not override_list and idx_reader is not None:
            try:
                overrides_map = idx_reader.get_overrides_for_path(path)
                # Case-insensitive lookup (Cyrillic)
                ov_lower = {k.lower(): v for k, v in overrides_map.items()}
                override_list = ov_lower.get(target["name"].lower())
            except Exception:
                override_list = None

        if not override_list:
            return body

        from rlm_tools_bsl.extension_detector import detect_extension_context as _det_ctx

        try:
            ext_context = _det_ctx(base_path)
        except Exception:
            return body

        trusted_roots: set[Path] = set()
        for e in ext_context.nearby_extensions:
            trusted_roots.add(Path(e.path).resolve())
        trusted_roots.add(Path(ext_context.current.path).resolve())

        parts = [body]
        for ov in override_list:
            ext_root = ov.get("extension_root", "")
            ext_mod = ov.get("ext_module_path", "")
            annotation = ov.get("annotation", "")
            ext_name = ov.get("extension_name", "")
            ext_method = ov.get("extension_method", "")
            ext_line = ov.get("ext_line")

            header = f'\n// === Перехвачен &{annotation} в расширении "{ext_name}" ==='
            file_ref = f"// Файл: {ext_name}/{ext_mod}"
            if ext_line:
                file_ref += f":{ext_line}"

            # Try to read extension method body
            ext_body = None
            if ext_root and ext_mod:
                candidate = Path(ext_root, ext_mod).resolve()
                if any(candidate.is_relative_to(root) for root in trusted_roots):
                    try:
                        ext_content = candidate.read_text(encoding="utf-8-sig", errors="replace")
                        ext_lines = ext_content.splitlines()
                        # Find method by name in extension file
                        proc_def_re = re.compile(BSL_PATTERNS["procedure_def"], re.IGNORECASE)
                        proc_end_re = re.compile(BSL_PATTERNS["procedure_end"], re.IGNORECASE)
                        search_name = (ext_method or "").lower()
                        in_target = False
                        start_idx = None
                        for i, ln in enumerate(ext_lines):
                            if not in_target:
                                m = proc_def_re.search(ln)
                                if m and m.group(2).lower() == search_name:
                                    in_target = True
                                    start_idx = i
                            else:
                                if proc_end_re.search(ln):
                                    ext_body = "\n".join(ext_lines[start_idx : i + 1])
                                    break
                        if in_target and ext_body is None and start_idx is not None:
                            ext_body = "\n".join(ext_lines[start_idx:])
                    except OSError:
                        pass

            parts.append(header)
            parts.append(file_ref)
            if ext_body:
                if numbered and start_idx is not None:
                    from rlm_tools_bsl._format import number_lines

                    ext_body = number_lines(ext_body, start=start_idx + 1)
                parts.append(ext_body)

        return "\n".join(parts)

    def find_callers(proc_name: str, module_hint: str = "", max_files: int = 20) -> list[dict]:
        """Find all callers of a procedure by name across BSL files.
        Delegates to find_callers_context for thorough cross-module search.

        Returns: list of dicts {file, line, text}."""
        result = find_callers_context(proc_name, module_hint, 0, max_files)
        return [{"file": c["file"], "line": c["line"], "text": c.get("context", "")} for c in result["callers"]]

    # --- Parallel prefilter for find_callers_context ---
    _base = Path(base_path)

    def _parallel_prefilter(
        files: list[tuple[str, BslFileInfo]],
        needle: str,
        base: str,
        max_workers: int = 12,
    ) -> list[tuple[str, BslFileInfo]]:
        """Scan all BSL files for substring in parallel using ThreadPoolExecutor.
        Bypasses sandbox read_file to avoid cache contention between threads.
        All paths come from the trusted index (built from glob inside base_path)."""
        base_p = Path(base)

        def _check(item: tuple[str, BslFileInfo]) -> tuple[str, BslFileInfo] | None:
            rel, info = item
            try:
                full = base_p / rel
                with open(full, "r", encoding="utf-8-sig", errors="replace") as f:
                    content = f.read()
                if needle in content.lower():
                    return (rel, info)
            except Exception:
                pass
            return None

        matched: list[tuple[str, BslFileInfo]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for result in pool.map(_check, files):
                if result is not None:
                    matched.append(result)
        return matched

    # --- Regex for stripping comments and string literals ---
    _re_string_literal = re.compile(r'"[^"\r\n]*"')

    def _strip_code_line(line: str) -> str:
        """Remove comments and string literals from a BSL code line."""
        # Strip string literals first (so "//" inside strings is not treated as comment)
        line = _re_string_literal.sub("", line)
        # Strip comment (// with or without space)
        ci = line.find("//")
        if ci >= 0:
            line = line[:ci]
        return line

    def find_callers_context(
        proc_name: str,
        module_hint: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Find callers of a procedure with full context: which procedure
        in which module calls the target. Returns structured result with
        caller_name, caller_is_export, file metadata, and pagination info.

        Unlike find_callers() which is a flat grep, this helper identifies
        the exact calling procedure and filters out comments/strings.
        Uses SQLite call graph index when available (instant).

        Args:
            proc_name: Name of the target procedure/function.
            module_hint: Optional module name to determine export scope.
            offset: File offset for pagination (0-based).
            limit: Max files to scan per call (default 50).

        Returns:
            dict with "callers" list and "_meta" pagination info.
        """
        # --- Fast path: SQLite call graph ---
        if idx_reader is not None and idx_reader.has_calls:
            _t0 = _time_mod.monotonic()
            result = idx_reader.get_callers(proc_name, module_hint, offset, limit)
            _elapsed = _time_mod.monotonic() - _t0
            if result is not None:
                _n = len(result.get("callers", []))
                logger.debug(
                    "find_callers_context: proc=%s source=index rows=%d time=%.2fs",
                    proc_name,
                    _n,
                    _elapsed,
                )
                if _n > 0:
                    return result
                if idx_zero_callers_authoritative:
                    logger.debug(
                        "find_callers_context: proc=%s index=0, authoritative=True, skip FS fallback",
                        proc_name,
                    )
                    result["_meta"]["fallback_skipped"] = True
                    result["_meta"]["hint"] = (
                        "No callers found in call index. Use safe_grep(proc_name) to search for text mentions."
                    )
                    return result
                # Untrusted/stale index — fall back to FS scan
                logger.debug(
                    "find_callers_context: proc=%s index returned 0, falling back to scan",
                    proc_name,
                )
            else:
                logger.debug(
                    "find_callers_context: proc=%s source=index returned_none time=%.2fs, falling back to scan",
                    proc_name,
                    _elapsed,
                )

        _ensure_index()

        name_esc = re.escape(proc_name)
        # Patterns: direct call, qualified call (Module.Proc)
        call_patterns = [
            re.compile(r"(?<!\w)" + name_esc + r"\s*\(", re.IGNORECASE),
            re.compile(r"\." + name_esc + r"\s*\(", re.IGNORECASE),
            re.compile(r"(?<!\w)" + name_esc + r"(?!\w)", re.IGNORECASE),
        ]

        # --- Step 1: Determine scope based on export status ---
        target_files: list[str] | None = None  # None = search all

        if module_hint:
            hint_modules = find_module(module_hint)
            if hint_modules:
                # Find the target procedure in hint modules
                for hm in hint_modules:
                    try:
                        procs = extract_procedures(hm["path"])
                        for p in procs:
                            if p["name"].lower() == proc_name.lower():
                                if not p["is_export"] or hm.get("form_name") is not None:
                                    # Not exported or form module -> only search same file
                                    target_files = [hm["path"]]
                                break
                    except Exception:
                        pass
                    if target_files is not None:
                        break

        # --- Step 2: Build candidate file list ---
        if target_files is not None:
            # Scoped to specific files (non-export or form)
            candidate_files = [(rel, info) for rel, info in _index_state if rel in target_files]
        else:
            candidate_files = list(_index_state)

        # --- Step 3: Prefilter by substring (parallel scan, cached) ---
        proc_lower = proc_name.lower()

        if target_files is not None:
            # Scoped search — don't use global prefilter cache
            filtered_files: list[tuple[str, BslFileInfo]] = []
            for rel, info in candidate_files:
                try:
                    content = _ext_read_file(rel)
                    if proc_lower in content.lower():
                        filtered_files.append((rel, info))
                except Exception:
                    pass
        else:
            filtered_files = _prefilter_lazy.get_or_set(
                proc_lower,
                lambda: _parallel_prefilter(candidate_files, proc_lower, base_path),
            )

        total_files = len(filtered_files)

        # --- Step 4: Apply pagination ---
        page_files = filtered_files[offset : offset + limit]
        scanned_files = len(page_files)

        # --- Step 5: Scan each file for callers ---
        callers: list[dict] = []

        for rel, info in page_files:
            try:
                content = _ext_read_file(rel)
                lines = content.splitlines()
                procs = extract_procedures(rel)

                for proc in procs:
                    # Skip the definition line itself
                    body_start = proc["line"]  # 1-based, this is the def line
                    body_end = proc["end_line"] if proc["end_line"] else len(lines)

                    for line_idx in range(body_start, body_end):  # body_start is def line (skip it)
                        if line_idx >= len(lines):
                            break
                        raw_line = lines[line_idx]
                        cleaned = _strip_code_line(raw_line)
                        if not cleaned.strip():
                            continue

                        for pattern in call_patterns:
                            if pattern.search(cleaned):
                                callers.append(
                                    {
                                        "file": rel,
                                        "caller_name": proc["name"],
                                        "caller_is_export": proc["is_export"],
                                        "line": line_idx + 1,  # 1-based
                                        "context": raw_line.rstrip(),
                                        "object_name": info.object_name,
                                        "category": info.category,
                                        "module_type": info.module_type,
                                    }
                                )
                                break  # one match per line is enough
            except Exception:
                pass

        logger.debug(
            "find_callers_context: proc=%s source=fallback callers=%d files_scanned=%d files_total=%d",
            proc_name,
            len(callers),
            scanned_files,
            total_files,
        )
        return {
            "callers": callers,
            "_meta": {
                "total_callers": len(callers),
                "returned": len(callers),
                "offset": offset,
                "has_more": (offset + limit) < total_files,
            },
        }

    def find_call_hierarchy(
        name: str,
        direction: str = "callers",
        depth: int = 2,
    ) -> dict:
        """Build multi-level call hierarchy. v1.10.0: only direction='callers'
        (uses existing idx_calls_callee). callees/both → structured error-dict.

        Args:
            name: Target procedure/function name.
            direction: 'callers' only in v1.10.0.
            depth: Levels to traverse (1..3, default 2).

        Returns:
            On success: {root, direction, depth, tree:[{name, callers:[...]}], visited}
              where each caller is {caller_name, module_path, category, object_name,
              line, is_export, level}.
            On unsupported direction: {error, hint, supported_directions}.
        """
        if direction not in ("callers", "callees", "both"):
            return {
                "error": f"Unknown direction: {direction!r}",
                "hint": "Use direction='callers' (the only supported direction in v1.10.0).",
                "supported_directions": ["callers"],
            }
        if direction != "callers":
            return {
                "error": f"Direction '{direction}' not supported in v1.10.0",
                "hint": (
                    "Use direction='callers' to find callers transitively. "
                    "For callees, the alternative is: extract_procedures(path) + "
                    "safe_grep over names in the procedure body."
                ),
                "supported_directions": ["callers"],
            }
        try:
            depth_int = int(depth)
        except (TypeError, ValueError):
            depth_int = 2
        depth_int = max(1, min(3, depth_int))

        result: dict = {
            "root": name,
            "direction": "callers",
            "depth": depth_int,
            "tree": [],
            "visited": 0,
            "truncated_targets": [],
        }

        # BFS by levels with cycle protection ON NAME (not on (name, level)).
        # Если cycle A → B → A: на уровне 1 обрабатываем A, на уровне 2 — B,
        # на уровне 3 пытаемся снова A. С protection (name, level) ключ (A, 3)
        # отличается от (A, 1) и A обрабатывается заново → дубликаты + ложное
        # ощущение «глубокого» дерева. С protection по имени — A обрабатывается
        # один раз, цикл корректно отсекается.
        visited_names: set[str] = set()
        queue: list[tuple[str, int]] = [(name, 1)]
        # Group callers by callee name for tree[i].
        by_target: dict[str, list[dict]] = {}
        # Аккумуляция truncated targets — find_callers_context имеет limit=200
        # и при популярных методах легко обрезается; молча терять данные нельзя.
        per_target_truncation: dict[str, dict] = {}

        while queue:
            target_name, level = queue.pop(0)
            name_key = target_name.lower()
            if name_key in visited_names:
                continue
            visited_names.add(name_key)
            result["visited"] += 1

            try:
                ctx = find_callers_context(target_name, module_hint="", offset=0, limit=200)
            except Exception:
                continue

            callers_list: list = []
            if isinstance(ctx, dict):
                callers_list = ctx.get("callers", []) or []
                meta = ctx.get("_meta", {}) or {}
                if meta.get("has_more"):
                    per_target_truncation[target_name] = {
                        "name": target_name,
                        "level": level,
                        "total": meta.get("total_callers"),
                        "returned": meta.get("returned"),
                    }

            for c in callers_list:
                caller_dict = {
                    "caller_name": c.get("caller_name", ""),
                    "module_path": c.get("file", ""),
                    "category": c.get("category", ""),
                    "object_name": c.get("object_name", ""),
                    "line": c.get("line", 0),
                    "is_export": bool(c.get("caller_is_export", False)),
                    "level": level,
                }
                by_target.setdefault(target_name, []).append(caller_dict)
                if level < depth_int:
                    next_name = c.get("caller_name", "")
                    if next_name and next_name.lower() not in visited_names:
                        queue.append((next_name, level + 1))

        for target_name, callers_list in by_target.items():
            result["tree"].append({"name": target_name, "callers": callers_list})
        result["truncated_targets"] = list(per_target_truncation.values())

        return result

    # XML file names by metadata category (CF format: Ext/<name>.xml)
    _CATEGORY_XML_NAMES = {
        "documents": "Document",
        "catalogs": "Catalog",
        "informationregisters": "RecordSet",
        "accumulationregisters": "RecordSet",
        "accountingregisters": "RecordSet",
        "calculationregisters": "RecordSet",
        "reports": "Report",
        "dataprocessors": "DataProcessor",
        "exchangeplans": "ExchangePlan",
        "chartsofaccounts": "ChartOfAccounts",
        "chartsofcharacteristictypes": "ChartOfCharacteristicTypes",
        "chartsofcalculationtypes": "ChartOfCalculationTypes",
        "businessprocesses": "BusinessProcess",
        "tasks": "Task",
        "constants": "Constant",
    }

    def _xml_candidates_named(object_name: str) -> list[str]:
        """Fast-path XML/MDO candidates: structural patterns + ext-metadata
        entries. No filesystem globs — keeps bulk ext scans (e.g.
        ``_live_attributes_in_extensions`` on 100+ ext objects) cheap.
        """
        parts = object_name.split("/")
        category = parts[0].lower() if parts else ""
        xml_name = _CATEGORY_XML_NAMES.get(category)
        last_segment = parts[-1] if parts else ""

        out: list[str] = []
        if last_segment:
            out.append(f"{object_name}/{last_segment}.mdo")
        if xml_name:
            out.append(f"{object_name}/Ext/{xml_name}.xml")
        out.append(f"{object_name}.xml")
        out.append(f"{object_name}.mdo")

        # Extension candidates from the metadata-XML pass — picks up XML-only
        # objects (Subsystems, EventSubscriptions) without a .bsl module.
        if _extension_metadata_xml and category and last_segment:
            target_cat = category.lower()
            target_name = last_segment.lower()
            for cat, obj_name, rel in _extension_metadata_xml:
                if cat.lower() == target_cat and obj_name.lower() == target_name:
                    out.append(rel)
        return out

    def _xml_candidates_glob_fallback(object_name: str) -> list[str]:
        """Slow-path glob fallback for non-standard layouts.

        Invoked ONLY when every named candidate from ``_xml_candidates_named``
        missed. Prior to this split the glob was unconditional, which on
        configs with many extensions (e.g. 197 ext objects on a 24K-BSL ERP)
        triggered 2× ``glob_files_fn`` calls per ext object inside
        ``_live_attributes_in_extensions`` — a runaway FS scan that could
        stall the session for tens of minutes. Now the glob fires only when
        a non-standard layout actually requires it.
        """
        out: list[str] = []
        try:
            ext_match = glob_files_fn(f"{object_name}/Ext/*.xml")
        except Exception:
            ext_match = []
        if ext_match:
            out.append(ext_match[0])
        try:
            mdo_match = glob_files_fn(f"{object_name}/*.mdo")
        except Exception:
            mdo_match = []
        if mdo_match:
            out.append(mdo_match[0])
        return out

    def _xml_candidates(object_name: str) -> list[str]:
        """Backwards-compatible wrapper combining named + glob fallback.
        Kept for any external callers; ``_resolve_object_xml`` now uses the
        two-tier helpers directly to avoid eager glob.
        """
        return _xml_candidates_named(object_name) + _xml_candidates_glob_fallback(object_name)

    def _resolve_object_xml(path: str) -> str:
        """Resolve path to the actual XML file.

        Accepts:
          - Direct path: 'Documents/Name/Ext/Document.xml' → as-is if exists
          - Directory path: 'Documents/Name' → tries Ext/<Type>.xml, then .xml, then .mdo
          - "Fake" file path: 'Documents/Name.mdo' / 'Documents/Name.xml'
            (no actual file at that exact location) → normalize base by stripping
            the extension and try the same candidate set as for a directory.

        Raises FileNotFoundError with an explicit hint when nothing resolves.
        """
        _ensure_index()  # ensure _extension_metadata_xml is populated

        normalized = path.replace("\\", "/")
        path_lower = normalized.lower()
        ends_with_xml = path_lower.endswith(".xml")
        ends_with_mdo = path_lower.endswith(".mdo")

        if ends_with_xml or ends_with_mdo:
            try:
                if _ext_resolve_safe(normalized).exists():
                    return normalized
            except Exception:
                pass
            # Fake .xml/.mdo path: normalize base (strip extension) and rebuild candidates.
            base = normalized[:-4]
        else:
            base = normalized

        if not base:
            raise FileNotFoundError(f"Path not found: {path!r}")

        parts = base.split("/")
        xml_name = _CATEGORY_XML_NAMES.get(parts[0].lower() if parts else "")
        last_segment = parts[-1] if parts else ""

        any_resolvable = False

        # Try named candidates first (no glob — fast path).
        for candidate in _xml_candidates_named(base):
            try:
                resolved = _ext_resolve_safe(candidate)
            except PermissionError:
                continue
            except Exception:
                continue
            any_resolvable = True
            try:
                if resolved.exists():
                    return candidate
            except OSError:
                continue

        # Slow path: glob fallback only when nothing named resolved. Critical
        # for bulk ext scans — see _xml_candidates_glob_fallback docstring.
        for candidate in _xml_candidates_glob_fallback(base):
            try:
                resolved = _ext_resolve_safe(candidate)
            except PermissionError:
                continue
            except Exception:
                continue
            any_resolvable = True
            try:
                if resolved.exists():
                    return candidate
            except OSError:
                continue

        if not any_resolvable:
            raise PermissionError(f"Access denied: path {path!r} escapes sandbox and extension roots")

        if ends_with_xml or ends_with_mdo:
            raise FileNotFoundError(
                f"Path not found: {path!r}. "
                f"Возможно вы передали '{path}' (фейковый файл). "
                f"Попробуйте '{base}' (директория) или '{base}/{last_segment}.mdo' (EDT) / "
                f"'{base}/Ext/{xml_name or '<Type>'}.xml' (CF)."
            )
        raise FileNotFoundError(
            f"Path not found: {path!r}. Use find_module('{last_segment}') to discover the correct path."
        )

    def parse_object_xml(path: str) -> dict:
        """Read a 1C metadata XML file and extract its structure:
        name, synonym, attributes, tabular sections, dimensions, resources,
        subsystem content. Works with any metadata XML (catalogs, documents,
        registers, subsystems, etc.).

        Accepts both direct XML paths and directory paths:
          parse_object_xml('Documents/Name/Ext/Document.xml')  — direct
          parse_object_xml('Documents/Name')                    — auto-resolves

        Returns: dict with keys like name, synonym, attributes, tabular_sections,
        dimensions, resources (depends on metadata type)."""
        resolved = _resolve_object_xml(path)
        content = _ext_read_file(resolved)
        return parse_metadata_xml(content)

    # ── Composite helpers (wrappers over existing functions) ────────

    def analyze_subsystem(name: str) -> dict:
        """Find a subsystem by name, parse its XML composition,
        classify objects as custom (non-standard prefix) or standard.

        Returns: dict with subsystems_found, subsystems list."""
        name = _strip_meta_prefix(name)

        # --- Fast path: SQLite index ---
        if idx_reader is not None:
            matches = idx_reader.get_subsystems_for_object(name)
            if matches is not None:
                # matches is [] or list of dicts
                results = []
                for m in matches:
                    results.append(
                        {
                            "file": m["file"],
                            "name": m["name"],
                            "synonym": m["synonym"],
                            "total_objects": len(m["matched_refs"]),
                            "matched_refs": m["matched_refs"],
                        }
                    )
                if not results:
                    return {
                        "error": f"Подсистема с '{name}' не найдена",
                        "hint": "Объект не входит ни в одну подсистему",
                    }
                return {"subsystems_found": len(results), "subsystems": results}

        # --- Fallback: glob + XML parse ---
        patterns = [
            f"**/Subsystems/**/*{name}*",
            f"**/Subsystems/*{name}*",
            # REMOVED: f"**/*{name}*.mdo" — scans entire tree, useless for subsystems
        ]
        found_files: list[str] = []
        for p in patterns:
            found_files.extend(glob_files_fn(p))

        subsystem_files = list(
            dict.fromkeys(f for f in found_files if "Subsystem" in f and (f.endswith(".xml") or f.endswith(".mdo")))
        )

        if not subsystem_files:
            return {
                "error": f"Подсистема '{name}' не найдена",
                "hint": "Попробуйте glob_files('**/Subsystems/**') для просмотра всех подсистем",
            }

        results = []
        for sf in subsystem_files:
            try:
                meta = parse_object_xml(sf)
            except Exception:
                continue
            if not meta or meta.get("object_type") != "Subsystem":
                continue

            content = meta.get("content", [])
            custom_objects = []
            standard_objects = []
            for item in content:
                parts = item.split(".", 1)
                obj_type = parts[0] if parts else ""
                obj_name = parts[1] if len(parts) > 1 else item
                is_custom = bool(obj_name) and obj_name[0].islower()
                entry = {"type": obj_type, "name": obj_name, "is_custom": is_custom}
                if is_custom:
                    custom_objects.append(entry)
                else:
                    standard_objects.append(entry)

            results.append(
                {
                    "file": sf,
                    "name": meta.get("name", ""),
                    "synonym": meta.get("synonym", ""),
                    "total_objects": len(content),
                    "custom_objects": custom_objects,
                    "standard_objects": standard_objects,
                    "raw_content": content,
                }
            )

        return {"subsystems_found": len(results), "subsystems": results}

    def find_custom_modifications(
        object_name: str,
        custom_prefixes: list[str] | None = None,
    ) -> dict:
        """Find all non-standard (custom) modifications in an object's modules:
        procedures with custom prefix, custom #Область regions, custom XML attributes.
        If custom_prefixes is not provided, uses auto-detected prefixes from the codebase.

        Returns: dict with modifications list and custom_attributes."""
        object_name = _strip_meta_prefix(object_name)
        prefix_source = "user" if custom_prefixes else "auto"
        prefixes = custom_prefixes or _ensure_prefixes()
        if not prefixes:
            return {"error": "Нетиповые префиксы не обнаружены. Укажите custom_prefixes вручную."}

        modules = find_module(object_name)
        exact = [m for m in modules if (m.get("object_name") or "").lower() == object_name.lower()]
        if not exact:
            exact = modules
        if not exact:
            return {"error": f"Объект '{object_name}' не найден"}

        def _match_prefix(s: str) -> bool:
            sl = s.lower()
            return any(sl.startswith(p.lower()) for p in prefixes)

        modifications = []
        for mod in exact:
            path = mod["path"]
            try:
                procs = extract_procedures(path)
            except Exception:
                continue

            custom_procs = [p for p in procs if _match_prefix(p["name"])]

            custom_regions: list[dict] = []
            try:
                content = _ext_read_file(path)
                for i, line in enumerate(content.splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#") and "Область" in stripped:
                        region_name = stripped.split("Область", 1)[1].strip()
                        if _match_prefix(region_name):
                            custom_regions.append({"name": region_name, "line": i})
            except Exception:
                pass

            if custom_procs or custom_regions:
                modifications.append(
                    {
                        "path": path,
                        "module_type": mod.get("module_type", ""),
                        "form_name": mod.get("form_name"),
                        "total_procedures": len(procs),
                        "custom_procedures": custom_procs,
                        "custom_regions": custom_regions,
                    }
                )

        custom_attributes: list[dict] = []
        parse_error: str | None = None
        category = exact[0].get("category", "")
        obj_name = exact[0].get("object_name", "")
        if category and obj_name:
            try:
                meta = parse_object_xml(f"{category}/{obj_name}")
                for attr in meta.get("attributes", []):
                    if _match_prefix(attr["name"]):
                        custom_attributes.append(attr)
                for ts in meta.get("tabular_sections", []):
                    if _match_prefix(ts["name"]):
                        custom_attributes.append(
                            {
                                "name": ts["name"],
                                "type": "TabularSection",
                                "synonym": ts.get("synonym", ""),
                            }
                        )
            except Exception as exc:
                parse_error = f"{type(exc).__name__}: {exc}"

        result = {
            "object_name": object_name,
            "prefixes_used": prefixes,
            "prefix_source": prefix_source,
            "modules_analyzed": len(exact),
            "modifications": modifications,
            "custom_attributes": custom_attributes,
        }
        if parse_error:
            result["parse_error"] = parse_error
        return result

    # ── Categories whose objects are pure metadata (no BSL module by default) ──
    # Used by _resolve_object_for_full_structure live-fallback to find XML-only
    # objects (Enums, FunctionalOptions, EventSubscriptions, etc.) when the index
    # is unavailable.
    _METADATA_ONLY_CATEGORIES = (
        "Enums",
        "Constants",
        "FunctionalOptions",
        "EventSubscriptions",
        "ScheduledJobs",
        "DefinedTypes",
        "ExchangePlans",
        "Subsystems",
        "Roles",
        "ChartsOfCharacteristicTypes",
        "ChartsOfAccounts",
        "ChartsOfCalculationTypes",
        # Categories that usually have modules but can also be XML-only:
        "Catalogs",
        "Documents",
        "InformationRegisters",
        "AccumulationRegisters",
        "AccountingRegisters",
        "CalculationRegisters",
        "BusinessProcesses",
        "Tasks",
        "Reports",
        "DataProcessors",
    )

    def _resolve_object_for_full_structure(name: str) -> tuple[str | None, str | None]:
        """Return (category, object_name) for a metadata object via a strict cascade.

        Pass 1 — exact-match через все источники по очереди (любой непустой
                 источник НЕ блокирует следующий, если в нём нет точного
                 совпадения):
            1. object_attributes (большинство объектов с реквизитами/ТЧ)
            2. object_synonyms via search_objects (synonym-only объекты)
            3. enum_values (Enums без записей в object_attributes)
            4. find_module (объекты с BSL-модулями)

        Pass 2 — live glob по известным метаданным категориям (всегда exact:
                 имя файла = name).

        Pass 3 — close-match fallback: если ВСЕ источники Pass 1 прошли без
                 exact-совпадения, вернуться к ним по тому же порядку и взять
                 первый non-empty. Воспроизводит старое поведение для случаев,
                 когда indexer положил объект под другим именем.

        Returns (None, None) если ничего не нашлось.

        v1.10.0 BUG-4 fix: ранее close-match из первого непустого источника
        (object_attributes c LIKE '%name%') блокировал exact-match в других
        источниках — БизнесПроцесс «Согласование» терялся за регистром-
        омонимом «тст_СогласованиеЗаявокСБ».
        """
        name_lower = name.lower()

        rows = None
        so_rows = None
        ev = None

        # ── Pass 1: exact-match через все источники ──────────────────────
        if idx_reader is not None:
            # 1. object_attributes — большинство объектов с реквизитами/ТЧ
            try:
                rows = idx_reader.get_object_attributes(object_name=name, limit=50)
            except Exception:
                rows = None
            for r in rows or []:
                if (r.get("object_name") or "").lower() == name_lower:
                    return r.get("category"), r.get("object_name")

            # 2. object_synonyms — synonym-only объекты (Enum/Constant/FO)
            try:
                so_rows = idx_reader.search_objects(name, limit=20)
            except Exception:
                so_rows = None
            for s in so_rows or []:
                if (s.get("object_name") or "").lower() == name_lower:
                    return s.get("category"), s.get("object_name")

            # 3. enum_values — Enum, у которого нет записей в object_synonyms
            try:
                ev = idx_reader.get_enum_values(name)
            except Exception:
                ev = None
            if ev and not ev.get("error") and ev.get("name") and ev["name"].lower() == name_lower:
                return "Enums", ev["name"]

        # 4. find_module — объекты с BSL-модулями (exact-проход)
        modules = find_module(name)
        exact_modules = [m for m in modules if (m.get("object_name") or "").lower() == name_lower]
        if exact_modules:
            return exact_modules[0].get("category"), exact_modules[0].get("object_name")

        # ── Pass 2: live glob по категориям (всегда exact, имя файла = name) ─
        for cat in _METADATA_ONLY_CATEGORIES:
            # Try CF directory layout: {Cat}/{name}/Ext/*.xml
            try:
                hits = glob_files_fn(f"{cat}/{name}/Ext/*.xml")
            except Exception:
                hits = []
            if hits:
                return cat, name
            # Try EDT layout: {Cat}/{name}/{name}.mdo
            try:
                hits = glob_files_fn(f"{cat}/{name}/{name}.mdo")
            except Exception:
                hits = []
            if hits:
                return cat, name
            # Try CF sibling-only layout: {Cat}/{name}.xml
            try:
                hits = glob_files_fn(f"{cat}/{name}.xml")
            except Exception:
                hits = []
            if hits:
                return cat, name

        # ── Pass 3: close-match fallback ────────────────────────────────
        # Все источники Pass 1 не дали exact — берём первый non-empty в
        # исходном порядке. Сохраняет прежнее поведение «get_enum_values
        # как close-match» (агент пишет 'Статус', в индексе Enum
        # 'СтатусыЗаказов' — substring-based get_enum_values его находит).
        if rows:
            first = rows[0]
            return first.get("category"), first.get("object_name")
        if so_rows:
            first = so_rows[0]
            return first.get("category"), first.get("object_name")
        if ev and not ev.get("error") and ev.get("name"):
            return "Enums", ev["name"]
        if modules:
            return modules[0].get("category"), modules[0].get("object_name")

        return None, None

    def get_object_full_structure(name: str) -> dict:
        """Aggregating helper: full object structure in one call.

        Combines metadata from object_attributes / predefined_items / object_synonyms /
        enum_values / form_elements (when index exists), with live XML fallback.
        Replaces the typical chain: parse_object_xml + find_attributes + find_predefined +
        find_enum_values per EnumRef.X.

        _meta semantics:
          index_used=True  означает «возвращённые в результате СТРУКТУРНЫЕ
                            секции (attributes, dimensions, resources,
                            tabular_sections, predefined_items) взяты из
                            индекса». Это контракт об ИСТОЧНИКЕ возвращаемых
                            данных, а НЕ об их полноте: если индекс stale
                            (например, часть TS не успела попасть в
                            object_attributes, но есть в XML), результат
                            вернёт только то, что есть в индексе, без чтения
                            live XML. Это сознательный performance-tradeoff —
                            хелпер не делает второй парсинг XML «ради
                            проверки полноты». Если агенту нужна гарантия
                            полноты — пусть дополнительно вызывает
                            parse_object_xml() или проверяет свежесть индекса
                            через get_index_info(). synonym/forms могут быть
                            подтянуты из live (они вспомогательные); synonym
                            самой ТЧ может быть обогащён из live по name
                            (это enrichment, а не замена структурных данных).
          index_used=False означает «хотя бы часть структуры пришла из live
                            XML». Причина — в fallback_reason:
            'index_unavailable_or_table_missing'        — индекса нет / таблицы нет.
            'index_empty_for_object'                    — индекс есть, но
                                                         object_attributes пустой
                                                         для нормальной категории
                                                         (stale/incomplete index).
            'category_without_attributes_filled_via_live_xml' — индекс есть, но
                                                         категория (Enum/Constant/...)
                                                         по природе не имеет attrs;
                                                         структура взята live.
            'index_partially_enriched_from_live_xml'    — индекс дал часть структуры,
                                                         live XML был вызван
                                                         (для synonym/forms/ts-synonym
                                                         enrichment) и ЗАОДНО
                                                         дозаполнил недостающие
                                                         структурные секции.
                                                         Источник смешанный. ВАЖНО:
                                                         этот reason возникает только
                                                         когда live в принципе
                                                         вызывался; если индекс дал
                                                         synonym+forms+attributes без
                                                         нужды в live, скрытые в XML
                                                         TS могут остаться
                                                         незамеченными — это всё ещё
                                                         index_used=True (см. выше).
            'parse_failed: ...'                         — live XML тоже не смог.

          ts_synonyms_available=True ставится ТОЛЬКО когда хотя бы у одной TS
          в результате есть непустой synonym (не просто факт «мы парсили live»).

        posting для документов:
          posting в индексе v12 не хранится. На чистом index path
          (index_used=True без enrichment) posting остаётся None — это согласовано
          с контрактом «без чтения live XML». Если live был вызван по другим
          причинам (synonym/forms/ts enrichment, fallback) — posting подхватывается
          из того же XML-чтения. Если posting нужен гарантированно, агенту
          следует использовать find_register_movements(doc_name): при пустом
          результате он сам делает live posting check.

        Returns dict:
          {object_name, category, synonym, posting,
           attributes, tabular_sections:[{name, synonym, columns}],
           dimensions, resources, predefined_items,
           enum_values_for_typed_refs:{Enum.X:[...]},
           forms:[str],
           _meta:{index_used:bool, fallback_reason:str|None, ts_synonyms_available:bool}}
        """
        name = _strip_meta_prefix(name)

        # --- Resolve (category, object_name) via metadata-first cascade ---
        # find_module() работает только по BSL-модулям, поэтому XML-only объекты
        # (Enums, Constants, многие Catalogs без ObjectModule, FunctionalOption и т.п.)
        # через него не находятся. Каскад: index metadata → index synonyms →
        # index enum_values → BSL modules → live glob по категориям.
        category, obj_name = _resolve_object_for_full_structure(name)
        if not category and not obj_name:
            return {
                "error": f"Объект '{name}' не найден",
                "_meta": {"index_used": False, "fallback_reason": "object_not_found", "ts_synonyms_available": False},
            }
        category = category or ""
        obj_name = obj_name or name

        result: dict = {
            "object_name": obj_name,
            "category": category,
            "synonym": None,
            "posting": None,
            "attributes": [],
            "tabular_sections": [],
            "dimensions": [],
            "resources": [],
            "predefined_items": [],
            "enum_values_for_typed_refs": {},
            "forms": [],
            "_meta": {
                "index_used": False,
                "fallback_reason": None,
                "ts_synonyms_available": False,
            },
        }

        # Категории, у которых ОБЪЕКТНЫХ атрибутов нет по природе:
        # для них пустой результат get_object_attributes — это норма, а не
        # признак "stale index". Не путать с _METADATA_ONLY_CATEGORIES, который
        # включает в т.ч. Catalogs/Documents — у них атрибуты есть.
        _CATEGORIES_WITHOUT_ATTRIBUTES = {
            "enums",
            "constants",
            "functionaloptions",
            "eventsubscriptions",
            "scheduledjobs",
            "definedtypes",
            "subsystems",
            "roles",
            "exchangeplans",  # имеют content вместо обычных атрибутов
        }

        def _populate_from_live_xml() -> str | None:
            """Read object via parse_object_xml and fill result fields.

            Возвращает None при успехе, текст ошибки при неудаче.

            Side-effects через `result["_meta"]`:
              - Если live дозаполнил СТРУКТУРНЫЕ секции (attributes, dimensions,
                resources, tabular_sections, predefined_items, которые были
                пустыми до вызова) — выставляется приватный маркер
                `_meta["_live_filled_structural"] = True`. Вызывающий код
                (index path) использует его чтобы понизить index_used=False
                с fallback_reason='index_partially_enriched_from_live_xml'.
              - `_meta["ts_synonyms_available"]` ставится True ТОЛЬКО когда после
                наполнения/обогащения у хотя бы одной TS есть НЕПУСТОЙ synonym.
            """
            try:
                meta = parse_object_xml(f"{category}/{obj_name}" if category else obj_name)
            except Exception as exc:
                return f"parse_failed: {type(exc).__name__}: {exc}"
            if not isinstance(meta, dict):
                return "parse_failed: non-dict result"

            structural_filled_from_live = False  # для понижения index_used

            # synonym / posting — НЕ структурные данные, обогащение не считается
            # за «mixed source».
            if not result["synonym"]:
                result["synonym"] = meta.get("synonym") or None
            if meta.get("posting") and not result.get("posting"):
                result["posting"] = meta["posting"]

            # --- Structural sections ---
            if not result["attributes"]:
                for attr in meta.get("attributes", []) or []:
                    result["attributes"].append(
                        {
                            "name": attr.get("name", ""),
                            "synonym": attr.get("synonym", "") or "",
                            "type": [attr.get("type", "")]
                            if isinstance(attr.get("type"), str)
                            else (attr.get("type") or []),
                        }
                    )
                if result["attributes"]:
                    structural_filled_from_live = True
            if not result["dimensions"]:
                for dim in meta.get("dimensions", []) or []:
                    result["dimensions"].append(
                        {
                            "name": dim.get("name", ""),
                            "synonym": dim.get("synonym", "") or "",
                            "type": [dim.get("type", "")]
                            if isinstance(dim.get("type"), str)
                            else (dim.get("type") or []),
                        }
                    )
                if result["dimensions"]:
                    structural_filled_from_live = True
            if not result["resources"]:
                for res_attr in meta.get("resources", []) or []:
                    result["resources"].append(
                        {
                            "name": res_attr.get("name", ""),
                            "synonym": res_attr.get("synonym", "") or "",
                            "type": [res_attr.get("type", "")]
                            if isinstance(res_attr.get("type"), str)
                            else (res_attr.get("type") or []),
                        }
                    )
                if result["resources"]:
                    structural_filled_from_live = True

            # --- Tabular sections: либо полное заполнение, либо обогащение synonym ---
            if not result["tabular_sections"]:
                # Index не дал TS — заполняем целиком из live (synonym у TS будет).
                for ts in meta.get("tabular_sections", []) or []:
                    result["tabular_sections"].append(
                        {
                            "name": ts.get("name", ""),
                            "synonym": ts.get("synonym", "") or None,
                            "columns": [
                                {
                                    "name": c.get("name", ""),
                                    "synonym": c.get("synonym", "") or "",
                                    "type": [c.get("type", "")]
                                    if isinstance(c.get("type"), str)
                                    else (c.get("type") or []),
                                }
                                for c in ts.get("attributes", []) or []
                            ],
                        }
                    )
                if result["tabular_sections"]:
                    structural_filled_from_live = True
            else:
                # TS уже из индекса — у них synonym=None. Обогащаем по name.
                live_ts_by_name = {(ts.get("name") or "").lower(): ts for ts in meta.get("tabular_sections", []) or []}
                for ts_in_result in result["tabular_sections"]:
                    name_key = (ts_in_result.get("name") or "").lower()
                    live_ts = live_ts_by_name.get(name_key)
                    if not live_ts:
                        continue
                    if not ts_in_result.get("synonym"):
                        new_syn = live_ts.get("synonym", "") or None
                        if new_syn:
                            ts_in_result["synonym"] = new_syn

            # ts_synonyms_available: True ТОЛЬКО если у хотя бы одной TS есть
            # реально непустой synonym (после полного заполнения / обогащения).
            if any(ts.get("synonym") for ts in result["tabular_sections"]):
                result["_meta"]["ts_synonyms_available"] = True

            # forms — НЕ структурные данные.
            forms = meta.get("forms")
            if forms and not result["forms"]:
                result["forms"] = list(forms)

            # predefined_items — структурные.
            if not result["predefined_items"]:
                try:
                    pi_results = find_predefined(object_name=f"{category}/{obj_name}" if category else obj_name)
                except Exception:
                    pi_results = []
                for item in pi_results or []:
                    result["predefined_items"].append(
                        {
                            "name": item.get("item_name", ""),
                            "synonym": item.get("item_synonym", "") or "",
                            "code": item.get("item_code", "") or "",
                            "types": item.get("types", []) or [],
                            "is_folder": item.get("is_folder", False),
                        }
                    )
                if result["predefined_items"]:
                    structural_filled_from_live = True

            if structural_filled_from_live:
                # Приватный маркер для index path: сигнал что live дозаполнил
                # структуру → нужно понизить index_used.
                result["_meta"]["_live_filled_structural"] = True
            return None

        # --- Index path ---
        attrs_rows: list[dict] | None = None
        if idx_reader is not None:
            try:
                attrs_rows = idx_reader.get_object_attributes(object_name=obj_name, category=category, limit=2000)
            except Exception:
                attrs_rows = None

        index_attempted = attrs_rows is not None  # таблица существует и доступна
        if index_attempted:
            # Group attributes by attr_kind / ts_name.
            ts_groups: dict[str, list[dict]] = {}
            for row in attrs_rows or []:
                kind = row.get("attr_kind") or ""
                attr_dict = {
                    "name": row.get("attr_name", ""),
                    "synonym": row.get("attr_synonym", "") or "",
                    "type": row.get("attr_type", []) or [],
                }
                if kind == "attribute":
                    result["attributes"].append(attr_dict)
                elif kind == "dimension":
                    result["dimensions"].append(attr_dict)
                elif kind == "resource":
                    result["resources"].append(attr_dict)
                elif kind == "ts_attribute":
                    ts_name = row.get("ts_name") or ""
                    ts_groups.setdefault(ts_name, []).append(attr_dict)

            for ts_name, columns in ts_groups.items():
                result["tabular_sections"].append(
                    {
                        "name": ts_name,
                        "synonym": None,  # TS synonym is not in object_attributes table
                        "columns": columns,
                    }
                )

            # Predefined items
            try:
                pi_rows = idx_reader.get_predefined_items(object_name=obj_name, limit=2000)
            except Exception:
                pi_rows = None
            if pi_rows:
                result["predefined_items"] = [
                    {
                        "name": r.get("item_name", ""),
                        "synonym": r.get("item_synonym", "") or "",
                        "code": r.get("item_code", "") or "",
                        "types": r.get("types", []) or [],
                        "is_folder": r.get("is_folder", False),
                    }
                    for r in pi_rows
                ]

            # Object synonym (object_synonyms table) — small targeted query via search_objects.
            try:
                so_rows = idx_reader.search_objects(obj_name, limit=20)
            except Exception:
                so_rows = None
            if so_rows:
                for s in so_rows:
                    if (s.get("object_name") or "").lower() == obj_name.lower() and (
                        not category or (s.get("category") or "").lower() == category.lower()
                    ):
                        result["synonym"] = s.get("synonym") or None
                        break

            # Forms from form_elements (distinct form_name).
            try:
                fe_rows = idx_reader.get_form_elements(object_name=obj_name)
            except Exception:
                fe_rows = None
            if fe_rows:
                seen_forms: list[str] = []
                for r in fe_rows:
                    fname = r.get("form_name") or ""
                    if fname and fname not in seen_forms:
                        seen_forms.append(fname)
                result["forms"] = seen_forms

            # --- Determine if index actually delivered STRUCTURAL data ---
            # Семантика _meta.index_used:
            #   True  ⇒ структурные данные (attributes / dimensions / resources /
            #            tabular_sections / predefined_items) взяты из индекса.
            #   False ⇒ структуру дал live XML (или объект — XML-only по природе).
            #
            # synonym и forms сюда НЕ входят: одна строка в object_synonyms
            # без записей в object_attributes — это «индекс знает что объект
            # есть», но не «индекс дал структуру». Для нормальных категорий
            # (Catalogs/Documents/Registers/...) это сигнал stale/incomplete index
            # → нужен live fallback.
            cat_lower = category.lower() if category else ""
            has_structural_index_data = bool(
                result["attributes"]
                or result["dimensions"]
                or result["resources"]
                or result["tabular_sections"]
                or result["predefined_items"]
            )

            if has_structural_index_data:
                # Структура реально взята из индекса.
                result["_meta"]["index_used"] = True
                # synonym/forms — вспомогательные. Если индекс их не дал
                # (например, object_synonyms не наполнен или это объект без форм),
                # дополним live XML.
                # Также: TS из object_attributes идут с synonym=None — таблица
                # не хранит синоним самой ТЧ. Если есть TS без synonym —
                # подтягиваем синонимы из live XML по совпадению name (см.
                # _populate_from_live_xml). Ошибки игнорируем — структура уже
                # на руках.
                ts_needs_synonyms = any(ts.get("synonym") is None for ts in result["tabular_sections"])
                if not result["synonym"] or not result["forms"] or ts_needs_synonyms:
                    _populate_from_live_xml()
                # Если live дозаполнил СТРУКТУРНЫЕ секции (например, индекс дал
                # attributes, но не TS — а live добавил TS), это уже не «чистый
                # index path». Понижаем index_used и сигнализируем смешанный
                # источник через специальный fallback_reason. Маркер
                # _live_filled_structural — приватный, удаляем после использования.
                if result["_meta"].pop("_live_filled_structural", False):
                    result["_meta"]["index_used"] = False
                    result["_meta"]["fallback_reason"] = "index_partially_enriched_from_live_xml"
            else:
                # Структурных данных индекс не дал. Идём в live XML fallback,
                # независимо от того, нашёлся ли synonym/forms — они остаются
                # как «бонус из индекса», но index_used=False, потому что
                # СТРУКТУРА (то ради чего вызывается этот хелпер) пришла live.
                result["_meta"]["index_used"] = False
                if cat_lower in _CATEGORIES_WITHOUT_ATTRIBUTES:
                    # XML-only категория (Enum/Constant/FunctionalOption/...) —
                    # пустой object_attributes здесь норма, не stale index.
                    # Помечаем отдельной причиной чтобы агент не паниковал.
                    result["_meta"]["fallback_reason"] = "category_without_attributes_filled_via_live_xml"
                else:
                    # Нормальная категория, но object_attributes пустой —
                    # признак stale/incomplete index для конкретного объекта.
                    result["_meta"]["fallback_reason"] = "index_empty_for_object"
                err = _populate_from_live_xml()
                if err:
                    # И live тоже не смог — фиксируем причину парсинга,
                    # перетирая category_without_attributes_filled_via_live_xml.
                    result["_meta"]["fallback_reason"] = err
                # Приватный маркер уже отражён через index_used=False и
                # явный fallback_reason — удаляем чтобы не утекал в API.
                result["_meta"].pop("_live_filled_structural", None)
        else:
            # --- Fallback: live XML parse (index unavailable / table missing) ---
            result["_meta"]["index_used"] = False
            result["_meta"]["fallback_reason"] = "index_unavailable_or_table_missing"
            err = _populate_from_live_xml()
            if err:
                result["_meta"]["fallback_reason"] = err
                result["_meta"].pop("_live_filled_structural", None)
                return result
            result["_meta"].pop("_live_filled_structural", None)

        # NOTE: posting для документов в индексе v12 не хранится. Если live XML
        # был вызван (для enrichment / fallback), posting подхватывается внутри
        # _populate_from_live_xml. На чистом index path без enrichment posting
        # остаётся None — это согласовано с контрактом «index_used=True
        # = без чтения live XML». Если posting нужен независимо от пути —
        # используй find_register_movements(doc_name): при пустом результате
        # он сам делает live posting check (см. Tier 1.2).

        # --- Expand enum-ref types → values ---
        # Принимаем три формы записи типа перечисления:
        #   EnumRef.X            — стандартный 1С-формат (CF/EDT)
        #   ПеречислениеСсылка.X — русскоязычный alias
        #   Enum.X               — канонизированный формат, который может появиться
        #                          в нормализованных таблицах метаданных
        _ENUM_TYPE_PREFIXES = ("EnumRef.", "ПеречислениеСсылка.", "Enum.")

        def _is_enum_ref(t) -> bool:
            return isinstance(t, str) and t.startswith(_ENUM_TYPE_PREFIXES)

        enum_refs: list[str] = []
        for attr_group in (
            result["attributes"],
            result["dimensions"],
            result["resources"],
        ):
            for a in attr_group:
                for t in a.get("type", []) or []:
                    if _is_enum_ref(t) and t not in enum_refs:
                        enum_refs.append(t)
        for ts in result["tabular_sections"]:
            for c in ts.get("columns", []) or []:
                for t in c.get("type", []) or []:
                    if _is_enum_ref(t) and t not in enum_refs:
                        enum_refs.append(t)

        for ref in enum_refs:
            short_name = ref.split(".", 1)[1] if "." in ref else ref
            try:
                ev = find_enum_values(short_name)
            except Exception:
                continue
            if isinstance(ev, dict) and not ev.get("error"):
                result["enum_values_for_typed_refs"][ref] = [
                    {"name": v.get("name", ""), "synonym": v.get("synonym", "") or ""} for v in (ev.get("values") or [])
                ]

        return result

    def analyze_object(name: str) -> dict:
        """Full object profile in one call: XML metadata + all modules + procedures + exports.

        Returns: dict with name, category, metadata, modules."""
        name = _strip_meta_prefix(name)
        modules = find_module(name)
        exact = [m for m in modules if (m.get("object_name") or "").lower() == name.lower()]
        if not exact:
            exact = modules[:20]
        if not exact:
            return {"error": f"Объект '{name}' не найден"}

        category = exact[0].get("category", "")
        obj_name = exact[0].get("object_name", "")

        metadata: dict = {}
        if category and obj_name:
            try:
                metadata = parse_object_xml(f"{category}/{obj_name}")
            except Exception:
                pass

        module_details = []
        for mod in exact:
            path = mod["path"]
            try:
                procs = extract_procedures(path)
                exports = [p for p in procs if p.get("is_export")]
            except Exception:
                procs, exports = [], []

            module_details.append(
                {
                    "path": path,
                    "module_type": mod.get("module_type", ""),
                    "form_name": mod.get("form_name"),
                    "procedures_count": len(procs),
                    "exports_count": len(exports),
                    "procedures": procs,
                    "exports": exports,
                }
            )

        return {
            "name": obj_name,
            "category": category,
            "metadata": metadata,
            "modules": module_details,
        }

    # ── Business-process helpers ─────────────────────────────────

    _event_sub_lazy = LazyList()

    def _build_event_subscriptions() -> list[dict]:
        files = glob_files_fn("**/EventSubscriptions/**/*.xml")
        files.extend(glob_files_fn("**/EventSubscriptions/**/*.mdo"))
        files = list(dict.fromkeys(files))
        result: list[dict] = []
        for f in files:
            try:
                content = read_file_fn(f)
            except Exception:
                continue
            parsed = parse_event_subscription_xml(content)
            if parsed is None:
                continue
            handler = parsed["handler"]
            parts = handler.rsplit(".", 1)
            handler_procedure = parts[-1] if parts else handler
            handler_module = ""
            if len(parts) > 1:
                module_part = parts[0]
                if module_part.startswith("CommonModule."):
                    module_part = module_part[len("CommonModule.") :]
                handler_module = module_part
            result.append(
                {
                    "name": parsed["name"],
                    "synonym": parsed["synonym"],
                    "source_types": parsed["source_types"],
                    "source_count": len(parsed["source_types"]),
                    "event": parsed["event"],
                    "handler": handler,
                    "handler_module": handler_module,
                    "handler_procedure": handler_procedure,
                    "file": f,
                }
            )
        return result

    def _ensure_event_subscriptions() -> list[dict]:
        return _event_sub_lazy.ensure(_build_event_subscriptions)

    def find_event_subscriptions(
        object_name: str = "",
        custom_only: bool = False,
        event_filter: list[str] | str | None = None,
        limit: int | None = None,
    ) -> list[dict] | dict:
        """Find event subscriptions, optionally filtered by object name and/or event.
        Shows what fires when an object is written/posted/deleted.
        Uses SQLite index when available (instant), falls back to XML parsing.

        Args:
            object_name: Object name to filter by (case-insensitive substring
                         match against source types). Empty = return all.
            custom_only: If True, return only subscriptions whose name starts
                         with a detected custom prefix (auto-detected from codebase).
            event_filter: List of event substrings (case-insensitive) — отбор
                          по полю event. None = без фильтра. ['BeforeWrite']
                          вернёт все подписки, у которых event содержит 'beforewrite'.
                          Допустима **одна строка** ('BeforeWrite') — она будет
                          автоматически обёрнута в [event_filter] (типичная ошибка
                          агентов: голая строка раньше итерировалась по символам
                          и матчила ВСЕ события).
            limit: Если задан, возврат становится top-level dict
                   {"subscriptions", "total", "returned", "has_more"}. Если None
                   (default) — возвращается list[dict] (контракт прежний).

        Returns:
            Default (limit is None): list[dict] of subscriptions.
            With limit: dict {"subscriptions": [...], "total": N, "returned": K,
                              "has_more": bool}."""
        if object_name:
            object_name = _strip_meta_prefix(object_name)

        # Normalize event_filter: голая строка → [строка]. Иначе Python итерирует
        # по символам ('BeforeWrite' → ['B','e',...]) и каждый одно-символьный
        # substring-matcher ловит почти все события — фильтр де-факто игнорируется.
        if isinstance(event_filter, str):
            event_filter = [event_filter] if event_filter else None

        # --- Fast path: SQLite index ---
        result: list[dict] | None = None
        if idx_reader is not None:
            idx_result = idx_reader.get_event_subscriptions(object_name, event_filter=event_filter)
            if idx_result is not None:
                if custom_only:
                    prefixes = _ensure_prefixes()
                    if prefixes:
                        idx_result = [s for s in idx_result if any(s["name"].lower().startswith(p) for p in prefixes)]
                result = idx_result

        if result is None:
            all_subs = _ensure_event_subscriptions()

            if not object_name:
                # Return without source_types to keep output compact
                result = [{k: v for k, v in s.items() if k != "source_types"} for s in all_subs]
            else:
                name_lower = object_name.lower()
                result = []
                for s in all_subs:
                    # Include subscriptions that explicitly list this object in source_types,
                    # OR subscriptions with empty source_types (source_count=0) — these apply
                    # to all objects of a given type (catch-all subscriptions).
                    if not s["source_types"]:
                        matched = True
                    else:
                        matched = any(name_lower in t.lower() for t in s["source_types"])
                    if matched:
                        result.append(dict(s))  # include source_types for filtered results

            if event_filter:
                evs_lower = [e.lower() for e in event_filter]
                result = [s for s in result if any(ev in (s.get("event", "") or "").lower() for ev in evs_lower)]

            if custom_only:
                prefixes = _ensure_prefixes()
                if prefixes:
                    result = [s for s in result if any(s["name"].lower().startswith(p) for p in prefixes)]

        if limit is None:
            return result

        # Paginated mode — return top-level dict.
        total = len(result)
        page = result[: max(0, int(limit))]
        return {
            "subscriptions": page,
            "total": total,
            "returned": len(page),
            "has_more": total > len(page),
        }

    _sched_job_lazy = LazyList()

    def _build_scheduled_jobs() -> list[dict]:
        files = glob_files_fn("**/ScheduledJobs/**/*.xml")
        files.extend(glob_files_fn("**/ScheduledJobs/**/*.mdo"))
        files = list(dict.fromkeys(files))
        result: list[dict] = []
        for f in files:
            try:
                content = read_file_fn(f)
            except Exception:
                continue
            parsed = parse_scheduled_job_xml(content)
            if parsed is None:
                continue
            method = parsed["method_name"]
            parts = method.rsplit(".", 1)
            handler_procedure = parts[-1] if parts else method
            handler_module = ""
            if len(parts) > 1:
                module_part = parts[0]
                if module_part.startswith("CommonModule."):
                    module_part = module_part[len("CommonModule.") :]
                handler_module = module_part
            result.append(
                {
                    "name": parsed["name"],
                    "synonym": parsed["synonym"],
                    "method_name": method,
                    "handler_module": handler_module,
                    "handler_procedure": handler_procedure,
                    "use": parsed["use"],
                    "predefined": parsed["predefined"],
                    "restart_on_failure": parsed["restart_on_failure"],
                    "file": f,
                }
            )
        return result

    def _ensure_scheduled_jobs() -> list[dict]:
        return _sched_job_lazy.ensure(_build_scheduled_jobs)

    def find_scheduled_jobs(name: str = "") -> list[dict]:
        """Find scheduled (background) jobs, optionally filtered by name.
        Uses SQLite index when available (instant), falls back to XML parsing.

        Args:
            name: Name substring to filter by (case-insensitive). Empty = all.

        Returns: list of dicts with name, synonym, method_name,
                 handler_module, handler_procedure, use, predefined, file."""
        if name:
            name = _strip_meta_prefix(name)

        # --- Fast path: SQLite index ---
        if idx_reader is not None:
            idx_result = idx_reader.get_scheduled_jobs(name)
            if idx_result is not None:
                return idx_result

        all_jobs = _ensure_scheduled_jobs()
        if not name:
            return all_jobs
        name_lower = name.lower()
        return [j for j in all_jobs if name_lower in j["name"].lower()]

    # ── Integration metadata helpers ─────────────────────────────

    def find_http_services(name: str = "") -> list[dict]:
        """Find HTTP services, optionally filtered by name.
        Uses SQLite index when available, falls back to XML parsing.

        Args:
            name: Name substring to filter by (case-insensitive). Empty = all.

        Returns: list of dicts with name, root_url, templates, file."""
        if name:
            name = _strip_meta_prefix(name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_result = idx_reader.get_http_services(name)
            if idx_result is not None:
                return idx_result

        # Fallback: glob + parse
        from rlm_tools_bsl.bsl_xml_parsers import parse_http_service_xml

        files = glob_files_fn("HTTPServices/**/*.xml") + glob_files_fn("HTTPServices/**/*.mdo")
        results: list[dict] = []
        for fp in files:
            content = read_file_fn(fp)
            if not content:
                continue
            parsed = parse_http_service_xml(content)
            if parsed and (not name or name.lower() in parsed["name"].lower()):
                parsed["file"] = fp if not os.path.isabs(fp) else os.path.relpath(fp, base_path).replace("\\", "/")
                results.append(parsed)
        return results

    def find_web_services(name: str = "") -> list[dict]:
        """Find web services (SOAP), optionally filtered by name.
        Uses SQLite index when available, falls back to XML parsing.

        Args:
            name: Name substring to filter by (case-insensitive). Empty = all.

        Returns: list of dicts with name, namespace, operations, file."""
        if name:
            name = _strip_meta_prefix(name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_result = idx_reader.get_web_services(name)
            if idx_result is not None:
                return idx_result

        # Fallback: glob + parse
        from rlm_tools_bsl.bsl_xml_parsers import parse_web_service_xml

        files = glob_files_fn("WebServices/**/*.xml") + glob_files_fn("WebServices/**/*.mdo")
        results: list[dict] = []
        for fp in files:
            content = read_file_fn(fp)
            if not content:
                continue
            parsed = parse_web_service_xml(content)
            if parsed and (not name or name.lower() in parsed["name"].lower()):
                parsed["file"] = fp if not os.path.isabs(fp) else os.path.relpath(fp, base_path).replace("\\", "/")
                results.append(parsed)
        return results

    def find_xdto_packages(name: str = "") -> list[dict]:
        """Find XDTO packages, optionally filtered by name.
        Uses SQLite index when available, falls back to XML parsing.

        Args:
            name: Name substring to filter by (case-insensitive). Empty = all.

        Returns: list of dicts with name, namespace, types, file."""
        if name:
            name = _strip_meta_prefix(name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_result = idx_reader.get_xdto_packages(name)
            if idx_result is not None:
                return idx_result

        # Fallback: glob + parse
        from rlm_tools_bsl.bsl_xml_parsers import parse_xdto_package_xml, parse_xdto_types

        files = glob_files_fn("XDTOPackages/**/*.xml") + glob_files_fn("XDTOPackages/**/*.mdo")
        results: list[dict] = []
        for fp in files:
            content = read_file_fn(fp)
            if not content:
                continue
            parsed = parse_xdto_package_xml(content)
            if parsed and (not name or name.lower() in parsed["name"].lower()):
                # For EDT: check sibling Package.xdto
                if fp.endswith(".mdo"):
                    xdto_path = os.path.join(os.path.dirname(fp), "Package.xdto")
                    try:
                        xdto_content = read_file_fn(xdto_path)
                    except Exception:
                        xdto_content = None
                    if xdto_content:
                        parsed["types"] = parse_xdto_types(xdto_content)
                parsed["file"] = fp if not os.path.isabs(fp) else os.path.relpath(fp, base_path).replace("\\", "/")
                results.append(parsed)
        return results

    def find_exchange_plan_content(name: str) -> list[dict]:
        """Find exchange plan content (objects registered for exchange).
        Always parses XML at runtime (no index table).

        Args:
            name: Exchange plan name.

        Returns: list of dicts with ref, auto_record."""
        name = _strip_meta_prefix(name)
        from rlm_tools_bsl.bsl_xml_parsers import parse_exchange_plan_content as _parse_ep

        def _valid_files(pattern: str) -> list[str]:
            """Glob and filter out hint strings."""
            return [f for f in glob_files_fn(pattern) if not f.startswith("[")]

        # EDT: .mdo file of the exchange plan itself (content is inline)
        # CF: Ext/Content.xml
        files = (
            _valid_files(f"ExchangePlans/{name}/*.mdo")
            + _valid_files(f"ExchangePlans/{name}/**/*.mdo")
            + _valid_files(f"ExchangePlans/{name}/**/*.xml")
        )
        if not files:
            # Try wildcard search across all exchange plans
            all_files = _valid_files("ExchangePlans/**/*.xml") + _valid_files("ExchangePlans/**/*.mdo")
            name_lower = name.lower()
            files = [f for f in all_files if name_lower in f.lower()]

        results: list[dict] = []
        seen_refs: set[str] = set()
        for fp in files:
            content = read_file_fn(fp)
            if not content:
                continue
            items = _parse_ep(content)
            for item in items:
                if item["ref"] not in seen_refs:
                    results.append(item)
                    seen_refs.add(item["ref"])
        return results

    def _check_document_postable(document_name: str) -> dict:
        """Live read of Document.posting via parse_object_xml.
        Returns {"is_postable": bool, "posting": "Allow|Deny|UseSelectively|None"}
        or empty dict when posting cannot be determined.

        UseSelectively means part of the document types post — НЕ ставим is_postable=False.
        Только Deny → is_postable=False.
        """
        try:
            meta = parse_object_xml(f"Documents/{document_name}")
        except Exception:
            return {}
        if not isinstance(meta, dict) or meta.get("object_type") != "Document":
            return {}
        posting = (meta.get("posting") or "").strip()
        if not posting:
            return {"posting": None}
        is_postable = posting.lower() != "deny"
        return {"posting": posting, "is_postable": is_postable}

    def find_register_movements(document_name: str) -> dict:
        """Find all registers that a document writes to during posting.
        Searches ObjectModule code for 'Движения.RegisterName' pattern.

        Args:
            document_name: Document name (or fragment).

        Returns: dict with document, code_registers, modules_scanned, и при пустом
                 итоговом результате — is_postable + hint, если документ непроводимый
                 (Posting=Deny в XML)."""
        document_name = _strip_meta_prefix(document_name)

        result: dict
        # Fast path: SQLite index
        if idx_reader is not None:
            idx_movements = idx_reader.get_register_movements(document_name)
            if idx_movements is not None:
                result = {
                    "document": document_name,
                    "code_registers": [
                        {"name": m["register_name"], "source": m["source"], "file": m["file"]}
                        for m in idx_movements
                        if m["source"] == "code"
                    ],
                    "modules_scanned": [],
                    "erp_mechanisms": [m["register_name"] for m in idx_movements if m["source"] == "erp_mechanism"],
                    "manager_tables": [m["register_name"] for m in idx_movements if m["source"] == "manager_table"],
                    "adapted_registers": [m["register_name"] for m in idx_movements if m["source"] == "adapted"],
                }
                _maybe_add_postability_hint(result, document_name)
                return result

        modules = find_by_type("Documents", document_name)
        obj_modules = [m for m in modules if m.get("module_type") == "ObjectModule"]

        if not obj_modules:
            result = {
                "document": document_name,
                "code_registers": [],
                "modules_scanned": [],
                "error": f"ObjectModule для документа '{document_name}' не найден",
            }
            _maybe_add_postability_hint(result, document_name)
            return result

        movement_re = re.compile(r"Движения\.(\w+)", re.IGNORECASE)
        code_registers: dict[str, dict] = {}  # name -> {name, lines, file}
        modules_scanned: list[str] = []

        for mod in obj_modules:
            path = mod["path"]
            modules_scanned.append(path)
            try:
                content = _ext_read_file(path)
            except Exception:
                continue
            for i, line in enumerate(content.splitlines(), 1):
                for m in movement_re.finditer(line):
                    reg_name = m.group(1)
                    if reg_name not in code_registers:
                        code_registers[reg_name] = {
                            "name": reg_name,
                            "lines": [],
                            "file": path,
                        }
                    if i not in code_registers[reg_name]["lines"]:
                        code_registers[reg_name]["lines"].append(i)

        result = {
            "document": document_name,
            "code_registers": list(code_registers.values()),
            "modules_scanned": modules_scanned,
        }

        # ── ERP framework fallback ──────────────────────────────
        # Look for ManagerModule to find ERP-style movement definitions
        mgr_modules = [m for m in modules if m.get("module_type") == "ManagerModule"]
        erp_mechanisms: list[str] = []
        manager_tables: list[str] = []
        adapted_registers: list[str] = []

        for mod in mgr_modules:
            mgr_path = mod["path"]
            try:
                mgr_content = _ext_read_file(mgr_path)
            except Exception:
                continue

            # ЗарегистрироватьУчетныеМеханизмы → МеханизмыДокумента.Добавить("X")
            mech_body = read_procedure(mgr_path, "ЗарегистрироватьУчетныеМеханизмы")
            if mech_body:
                mech_re = re.compile(r'МеханизмыДокумента\.Добавить\("(\w+)"\)', re.IGNORECASE)
                for m in mech_re.finditer(mech_body):
                    if m.group(1) not in erp_mechanisms:
                        erp_mechanisms.append(m.group(1))

            # ТекстЗапросаТаблицаXxx function names
            table_re = re.compile(r"(?:Функция|Процедура)\s+ТекстЗапросаТаблица(\w+)\s*\(", re.IGNORECASE)
            for m in table_re.finditer(mgr_content):
                table_name = m.group(1)
                if table_name not in manager_tables:
                    manager_tables.append(table_name)

            # АдаптированныйТекстЗапросаДвиженийПоРегистру → ИмяРегистра = "X"
            adapted_body = read_procedure(mgr_path, "АдаптированныйТекстЗапросаДвиженийПоРегистру")
            if adapted_body:
                reg_re = re.compile(r'ИмяРегистра\s*=\s*"(\w+)"', re.IGNORECASE)
                for m in reg_re.finditer(adapted_body):
                    if m.group(1) not in adapted_registers:
                        adapted_registers.append(m.group(1))

        result["erp_mechanisms"] = erp_mechanisms
        result["manager_tables"] = manager_tables
        result["adapted_registers"] = adapted_registers

        _maybe_add_postability_hint(result, document_name)
        return result

    def _maybe_add_postability_hint(result: dict, document_name: str) -> None:
        """If the combined result has no register movements at all,
        live-read Document.posting from XML and annotate the result when posting=Deny.
        Полный итог: code_registers + erp_mechanisms + manager_tables + adapted_registers == [].
        """
        empty = (
            not result.get("code_registers")
            and not result.get("erp_mechanisms")
            and not result.get("manager_tables")
            and not result.get("adapted_registers")
        )
        if not empty:
            return
        info = _check_document_postable(document_name)
        if not info:
            return
        # info contains: posting (str | None) and optionally is_postable (bool)
        if info.get("posting"):
            result["posting"] = info["posting"]
        if info.get("is_postable") is False:
            result["is_postable"] = False
            result["hint"] = (
                "Документ непроводимый (Posting=Deny) — движений регистров нет. "
                "Связь с регистрами ищите через find_event_subscriptions / "
                "регистры сведений с типом источника = документ."
            )

    def find_register_writers(register_name: str) -> dict:
        """Find all documents that write to a specific register.
        Searches all document ObjectModules for 'Движения.RegisterName'.

        Args:
            register_name: Register name to search for.

        Returns: dict with register, writers, total_documents_scanned, total_writers."""
        register_name = _strip_meta_prefix(register_name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_writers = idx_reader.get_register_writers(register_name)
            if idx_writers is not None:
                return {
                    "register": register_name,
                    "writers": [
                        {"document": w["document_name"], "source": w["source"], "file": w["file"]} for w in idx_writers
                    ],
                    "total_documents_scanned": 0,
                    "total_writers": len(idx_writers),
                }

        _ensure_index()
        # Collect all document ObjectModule files
        doc_modules = [
            (rel, info)
            for rel, info in _index_state
            if info.category and info.category.lower() == "documents" and info.module_type == "ObjectModule"
        ]

        needle = f"движения.{register_name}".lower()
        matched = _parallel_prefilter(doc_modules, needle, base_path)

        movement_re = re.compile(r"Движения\." + re.escape(register_name), re.IGNORECASE)
        writers: list[dict] = []
        for rel, info in matched:
            try:
                content = _ext_read_file(rel)
            except Exception:
                continue
            lines: list[int] = []
            for i, line in enumerate(content.splitlines(), 1):
                if movement_re.search(line):
                    lines.append(i)
            if lines:
                writers.append(
                    {
                        "document": info.object_name or "",
                        "file": rel,
                        "lines": lines,
                    }
                )

        return {
            "register": register_name,
            "writers": writers,
            "total_documents_scanned": len(doc_modules),
            "total_writers": len(writers),
        }

    def analyze_document_flow(document_name: str) -> dict:
        """Full document lifecycle analysis: metadata, event subscriptions,
        register movements, related scheduled jobs, based-on, print forms.

        v1.10.0 enrichment (BUG-6 fix): для непроводимых документов добавлены
        top-level is_postable=False + hint, чтобы агенту не нужно было лезть
        внутрь register_movements; based_on/print_forms обогащают результат
        для всех документов.

        Args:
            document_name: Document name (or fragment).

        Returns: dict with document, metadata, event_subscriptions,
                 register_movements, related_scheduled_jobs, based_on,
                 print_forms; для непроводимых дополнительно is_postable+hint."""
        document_name = _strip_meta_prefix(document_name)
        obj = analyze_object(document_name)
        subs = find_event_subscriptions(document_name)
        movements = find_register_movements(document_name)

        # Find scheduled jobs referencing this document
        all_jobs = find_scheduled_jobs()
        doc_lower = document_name.lower()
        related_jobs = [
            j
            for j in all_jobs
            if doc_lower in j.get("method_name", "").lower() or doc_lower in j.get("name", "").lower()
        ]

        # Composition graceful-degrade — каждый суб-вызов в try/except,
        # чтобы один сломавшийся хелпер не валил весь analyze_document_flow.
        try:
            based_on = find_based_on_documents(document_name)
        except Exception as exc:
            based_on = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            print_forms_data = find_print_forms(document_name)
        except Exception as exc:
            print_forms_data = {"error": f"{type(exc).__name__}: {exc}"}

        result: dict = {
            "document": obj.get("name", document_name),
            "metadata": obj.get("metadata", {}),
            "event_subscriptions": subs,
            "register_movements": movements,
            "related_scheduled_jobs": related_jobs,
            "based_on": based_on,
            "print_forms": print_forms_data,
        }

        # Top-level is_postable+hint для непроводимых — повторяем сигнал из
        # register_movements на верхний уровень для удобства агента.
        if isinstance(movements, dict) and movements.get("is_postable") is False:
            result["is_postable"] = False
            result["hint"] = (
                "Документ непроводимый (Posting=Deny). register_movements ожидаемо пустой. "
                "Связь с регистрами — через event_subscriptions, based_on "
                "или регистры сведений с типом-источником = документ."
            )

        return result

    # ── Based-on documents / Print forms helpers ───────────────

    def find_based_on_documents(document_name: str) -> dict:
        """Find what documents can be created FROM this document and what it can be created FROM.

        Прямой обход:
          - can_create_from_here:  ManagerModule.ДобавитьКомандыСозданияНаОсновании самого документа.
          - can_be_created_from:   ObjectModule.ОбработкаЗаполнения самого документа.

        Обратный обход (v1.10.x — BUG-9 fix): если прямой обход НИЧЕГО не нашёл
        для can_create_from_here (типичный кейс — Письма в ДО3: у них нет
        ДобавитьКомандыСозданияНаОсновании, но другие документы — Задача,
        Поручение и т.п. — декларируют ДокументСсылка.ВходящееПисьмо в своих
        ОбработкаЗаполнения), сканируется ОбработкаЗаполнения всех остальных
        ObjectModule в Documents/ и собираются документы, у которых ссылка на
        наш `document_name` есть в этой процедуре. Записи помечаются
        `"via": "back_scan"`.

        Returns: dict with document, can_create_from_here, can_be_created_from."""
        document_name = _strip_meta_prefix(document_name)
        result: dict = {
            "document": document_name,
            "can_create_from_here": [],
            "can_be_created_from": [],
        }

        modules = find_by_type("Documents", document_name)

        # --- ManagerModule: ДобавитьКомандыСозданияНаОсновании ---
        mgr_modules = [m for m in modules if m.get("module_type") == "ManagerModule"]
        for mod in mgr_modules:
            path = mod["path"]
            body = read_procedure(path, "ДобавитьКомандыСозданияНаОсновании")
            if body:
                create_re = re.compile(r"Документы\.(\w+)\.ДобавитьКоманду\w*НаОснован", re.IGNORECASE)
                for m in create_re.finditer(body):
                    result["can_create_from_here"].append(
                        {
                            "document": m.group(1),
                            "file": path,
                        }
                    )

        # --- ObjectModule: ОбработкаЗаполнения ---
        obj_modules = [m for m in modules if m.get("module_type") == "ObjectModule"]
        for mod in obj_modules:
            path = mod["path"]
            body = read_procedure(path, "ОбработкаЗаполнения")
            if body:
                type_re = re.compile(r'Тип\("(\w+Ссылка\.\w+)"\)', re.IGNORECASE)
                for m in type_re.finditer(body):
                    result["can_be_created_from"].append(
                        {
                            "type": m.group(1),
                            "file": path,
                        }
                    )

        # --- Reverse scan для can_create_from_here ---
        # Только если прямой обход ничего не нашёл — иначе дёшево пропускаем.
        if not result["can_create_from_here"]:
            try:
                obj_paths = glob_files_fn("Documents/*/Ext/ObjectModule.bsl") or []
            except Exception:
                obj_paths = []

            doc_lower = document_name.lower()
            # Pattern: ДокументСсылка.<our_name> с границей слова, case-insensitive.
            ref_re = re.compile(rf"ДокументСсылка\.{re.escape(document_name)}\b", re.IGNORECASE)
            seen: set[str] = set()
            for raw_path in obj_paths:
                # Path может прийти с разными разделителями (Windows/POSIX) — нормализуем.
                segs = raw_path.replace("\\", "/").split("/")
                if len(segs) < 2 or segs[0] != "Documents":
                    continue
                other = segs[1]
                # Пропускаем ObjectModule самого документа (он уже обработан в прямом обходе)
                # и дубли по object_name.
                if other.lower() == doc_lower or other in seen:
                    continue
                try:
                    body = read_procedure(raw_path, "ОбработкаЗаполнения")
                except Exception:
                    continue
                if not body or not ref_re.search(body):
                    continue
                seen.add(other)
                result["can_create_from_here"].append(
                    {
                        "document": other,
                        "file": raw_path,
                        "via": "back_scan",
                    }
                )

        return result

    def find_print_forms(object_name: str) -> dict:
        """Find print forms registered for an object by parsing ДобавитьКомандыПечати in ManagerModule.

        Returns: dict with object, print_forms list."""
        object_name = _strip_meta_prefix(object_name)
        result: dict = {
            "object": object_name,
            "print_forms": [],
        }

        modules = find_by_type("Documents", object_name)
        mgr_modules = [m for m in modules if m.get("module_type") == "ManagerModule"]
        if not mgr_modules:
            # Try broader search (Catalogs, DataProcessors, etc.)
            modules = find_module(object_name)
            mgr_modules = [m for m in modules if m.get("module_type") == "ManagerModule"]

        for mod in mgr_modules:
            path = mod["path"]
            body = read_procedure(path, "ДобавитьКомандыПечати")
            if body:
                # Pattern 1: helper-function style (ERP 1.x / UPP)
                #   ДобавитьКомандуПечати(КомандыПечати, "Ид", НСтр("ru = 'Представление'"))
                print_re = re.compile(
                    r'ДобавитьКомандуПечати\([^,]+,\s*"(\w+)"(?:,\s*НСтр\("ru\s*=\s*\'([^\']+)\')?',
                    re.IGNORECASE,
                )
                for m in print_re.finditer(body):
                    result["print_forms"].append(
                        {
                            "name": m.group(1),
                            "presentation": m.group(2) or "",
                            "file": path,
                        }
                    )

                # Pattern 2: property-style (ERP 2.x)
                #   КомандаПечати.Идентификатор = "Ид";
                #   КомандаПечати.Представление = НСтр("ru = 'Текст'");
                seen_ids = {pf["name"] for pf in result["print_forms"]}
                id_re = re.compile(
                    r'КомандаПечати\.Идентификатор\s*=\s*"(\w+)"',
                    re.IGNORECASE,
                )
                pres_re = re.compile(
                    r"КомандаПечати\.Представление\s*=\s*НСтр\(\"ru\s*=\s*'([^']+)'",
                    re.IGNORECASE,
                )
                ids = id_re.findall(body)
                presentations = pres_re.findall(body)
                for i, name in enumerate(ids):
                    if name not in seen_ids:
                        result["print_forms"].append(
                            {
                                "name": name,
                                "presentation": presentations[i] if i < len(presentations) else "",
                                "file": path,
                            }
                        )
                        seen_ids.add(name)

        return result

    # ── Form XML parsing helper ──────────────────────────────────

    def parse_form(object_name: str, form_name: str = "", handler: str = "") -> list[dict]:
        """Form event handlers, commands and attributes for an object's forms.

        Without form_name — all forms of the object. With form_name — specific form.
        handler='ProcName' — reverse lookup: find what a BSL procedure is bound to.

        Returns: list of dicts grouped by form, each with:
            category, object_name, form_name, file, module_path,
            handlers, commands, attributes."""
        object_name = _strip_meta_prefix(object_name)
        if not object_name:
            raise ValueError("object_name is required, e.g. parse_form('РеализацияТоваровУслуг')")

        # --- Fast path: SQLite index ---
        if idx_reader is not None:
            # Query ALL rows for the object/form (no handler filter at SQL level).
            # handler filters the SET of forms in _group_form_rows, but inside
            # each form commands/attributes stay complete for context.
            raw = idx_reader.get_form_elements(object_name, form_name)
            if raw is not None and raw:
                return _group_form_rows(raw, handler)
            # raw == [] means table exists but no rows — fall through to live
            # path so that empty forms (zero elements) are still discoverable.

        # --- Fallback: path-heuristic discovery ---
        from rlm_tools_bsl.bsl_xml_parsers import parse_form_xml as _parse_form_xml

        form_files: list[tuple[str, str, str, str]] = []  # (cat, obj, frm, rel_path)

        # Check CommonForms first (object_name = form_name)
        for pattern in (
            f"CommonForms/{object_name}/Form.form",
            f"CommonForms/{object_name}/Ext/Form.xml",
        ):
            found = glob_files_fn(pattern)
            for fp in found:
                form_files.append(("CommonForms", object_name, object_name, fp))

        # Standard categories
        from rlm_tools_bsl.format_detector import METADATA_CATEGORIES

        for cat in METADATA_CATEGORIES:
            if cat in ("CommonForms", "CommonModules", "CommonCommands", "CommonTemplates"):
                continue
            for pattern in (
                f"{cat}/{object_name}/Forms/*/Form.form",
                f"{cat}/{object_name}/Forms/*/Ext/Form.xml",
            ):
                found = glob_files_fn(pattern)
                for fp in found:
                    parts = fp.replace("\\", "/").split("/")
                    try:
                        fi = parts.index("Forms")
                        frm = parts[fi + 1]
                    except (ValueError, IndexError):
                        frm = ""
                    form_files.append((cat, object_name, frm, fp))

        # Last resort: broad glob
        if not form_files:
            for pattern in ("**/Forms/*/Form.form", "**/Forms/*/Ext/Form.xml"):
                found = glob_files_fn(pattern)
                for fp in found:
                    if object_name.lower() in fp.lower():
                        parts = fp.replace("\\", "/").split("/")
                        try:
                            fi = parts.index("Forms")
                            frm = parts[fi + 1]
                            obj = parts[fi - 1] if fi > 0 else object_name
                            c = parts[fi - 2] if fi > 1 else ""
                        except (ValueError, IndexError):
                            frm, obj, c = "", object_name, ""
                        form_files.append((c, obj, frm, fp))

        if form_name:
            form_files = [(c, o, f, p) for c, o, f, p in form_files if f == form_name]

        results: list[dict] = []
        for cat, obj, frm, fp in form_files:
            content = read_file_fn(fp)
            if not content:
                continue
            parsed = _parse_form_xml(content)
            if parsed is None:
                continue

            rel = fp if not os.path.isabs(fp) else os.path.relpath(fp, base_path).replace("\\", "/")
            full_fp = fp if os.path.isabs(fp) else os.path.join(base_path, fp)

            # Determine module_path
            module_path = ""
            if full_fp.replace("\\", "/").endswith("Ext/Form.xml"):
                # CF: Ext/Form.xml → module at Ext/Form/Module.bsl
                form_dir = os.path.dirname(full_fp)
                _candidates: tuple[str, ...] = ("Form/Module.bsl", "Module.bsl")
            else:
                form_dir = os.path.dirname(full_fp)
                _candidates = ("Ext/Module.bsl", "Module.bsl")
            for candidate in _candidates:
                mp = os.path.join(form_dir, candidate)
                if os.path.isfile(mp):
                    module_path = os.path.relpath(mp, base_path).replace("\\", "/")
                    break

            hs = parsed.get("handlers", [])
            if handler:
                hs = [h for h in hs if h["handler"].lower() == handler.lower()]
                if not hs:
                    continue

            results.append(
                {
                    "category": cat,
                    "object_name": obj,
                    "form_name": frm,
                    "file": rel,
                    "module_path": module_path,
                    "handlers": hs,
                    "commands": parsed.get("commands", []),
                    "attributes": parsed.get("attributes", []),
                }
            )

        return results

    def _group_form_rows(raw_rows: list[dict], handler_filter: str = "") -> list[dict]:
        """Group raw form_elements rows into per-form dicts."""
        forms: dict[tuple[str, str, str], dict] = {}
        for r in raw_rows:
            key = (r["category"], r["object_name"], r["form_name"])
            if key not in forms:
                # Derive module_path from file path
                file_path = r.get("file", "")
                module_path = ""
                if file_path:
                    if file_path.endswith("Form.form"):
                        # EDT: Form.form → Module.bsl in same dir
                        mp = file_path.rsplit("/", 1)[0] + "/Module.bsl"
                    elif file_path.endswith("Form.xml"):
                        # CF: Ext/Form.xml → Ext/Form/Module.bsl
                        mp = file_path.rsplit("/", 1)[0] + "/Form/Module.bsl"
                    else:
                        mp = ""
                    # Check if exists via glob
                    if mp:
                        found = glob_files_fn(mp)
                        module_path = mp if found else ""

                forms[key] = {
                    "category": r["category"],
                    "object_name": r["object_name"],
                    "form_name": r["form_name"],
                    "file": file_path,
                    "module_path": module_path,
                    "handlers": [],
                    "commands": [],
                    "attributes": [],
                }

            form = forms[key]
            kind = r.get("kind", "")
            if kind == "handler":
                h = {
                    "element": r.get("element_name", ""),
                    "event": r.get("event", ""),
                    "handler": r.get("handler", ""),
                    "element_type": r.get("element_type", ""),
                    "data_path": r.get("data_path", ""),
                    "scope": r.get("scope", ""),
                }
                if handler_filter:
                    if h["handler"].lower() == handler_filter.lower():
                        form["handlers"].append(h)
                else:
                    form["handlers"].append(h)
            elif kind == "command":
                form["commands"].append(
                    {
                        "name": r.get("element_name", ""),
                        "action": r.get("handler", ""),
                    }
                )
            elif kind == "attribute":
                attr: dict = {
                    "name": r.get("element_name", ""),
                    "types": r.get("element_type", ""),
                    "main": bool(r.get("attribute_is_main", 0)),
                }
                mt = r.get("main_table", "")
                if mt:
                    attr["main_table"] = mt
                extra = r.get("extra_json", "")
                if extra:
                    try:
                        ex = json.loads(extra)
                        qt = ex.get("query_text", "")
                        if qt:
                            attr["query_text"] = qt
                    except (json.JSONDecodeError, TypeError):
                        pass
                form["attributes"].append(attr)

        # Filter out forms with no matching handlers when handler_filter is set
        result = list(forms.values())
        if handler_filter:
            result = [f for f in result if f["handlers"]]
        return result

    # ── Enum / FunctionalOption / Roles helpers ──────────────────

    def find_enum_values(enum_name: str) -> dict:
        """Find an enumeration by name and return its values.

        Args:
            enum_name: Enum name (or fragment).

        Returns: dict with name, synonym, values, file — or error."""
        enum_name = _strip_meta_prefix(enum_name)

        # --- Fast path: SQLite index ---
        if idx_reader is not None:
            result = idx_reader.get_enum_values(enum_name)
            if result is not None:
                return result

        # --- Fallback: glob + XML parse ---
        patterns = [
            f"**/Enums/**/*{enum_name}*.xml",
            f"**/Enums/**/*{enum_name}*.mdo",
        ]
        found_files: list[str] = []
        for p in patterns:
            found_files.extend(glob_files_fn(p))
        found_files = list(dict.fromkeys(found_files))

        for f in found_files:
            try:
                content = read_file_fn(f)
            except Exception:
                continue
            parsed = parse_enum_xml(content)
            if parsed is None:
                continue
            if enum_name.lower() in parsed["name"].lower():
                parsed["file"] = f
                return parsed

        return {"error": f"Перечисление '{enum_name}' не найдено"}

    # Predefined items only exist for these categories (CF + EDT).
    _PREDEFINED_CATS = frozenset(
        ("Catalogs", "ChartsOfCharacteristicTypes", "ChartsOfAccounts", "ChartsOfCalculationTypes")
    )

    def _build_ext_attrs_cache() -> None:
        """Parse all attribute-bearing ext objects once per session.

        Builds ``_ext_attrs_cache`` keyed by ``(cat_lower, obj_lower)`` with
        rows of the same shape ``find_attributes`` returns (with ``source_file``).
        Subsequent ``find_attributes(name=…)`` calls then filter the cache
        in-memory — no XML re-parsing per ext object. Critical for large
        extensions (~150+ objects) where the cold scan takes 5-15s.
        """
        if _ext_attrs_cache_built[0]:
            return
        with _ext_attrs_cache_lock:
            if _ext_attrs_cache_built[0]:
                return
            if not _extension_metadata_xml:
                _ext_attrs_cache_built[0] = True
                return

            from rlm_tools_bsl.bsl_xml_parsers import normalize_type_string as _nts

            def _make_type(raw: str) -> list[str]:
                try:
                    return json.loads(_nts(raw))
                except Exception:
                    return []

            seen: set[tuple[str, str]] = set()
            for cat, obj_name, _rel in _extension_metadata_xml:
                if cat.lower() not in _CATEGORY_XML_NAMES:
                    continue
                key = (cat.lower(), obj_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                object_path = f"{cat}/{obj_name}"
                try:
                    resolved = _resolve_object_xml(object_path)
                    content = _ext_read_file(resolved)
                    parsed = parse_metadata_xml(content)
                except Exception:
                    continue
                if not parsed:
                    continue

                rows: list[dict] = []
                for attr in parsed.get("attributes", []):
                    rows.append(
                        {
                            "object_name": obj_name,
                            "category": cat,
                            "attr_name": attr.get("name", ""),
                            "attr_synonym": attr.get("synonym", ""),
                            "attr_type": _make_type(attr.get("type", "")),
                            "attr_kind": "attribute",
                            "ts_name": None,
                            "source_file": resolved,
                        }
                    )
                for dim in parsed.get("dimensions", []):
                    rows.append(
                        {
                            "object_name": obj_name,
                            "category": cat,
                            "attr_name": dim.get("name", ""),
                            "attr_synonym": dim.get("synonym", ""),
                            "attr_type": _make_type(dim.get("type", "")),
                            "attr_kind": "dimension",
                            "ts_name": None,
                            "source_file": resolved,
                        }
                    )
                for res in parsed.get("resources", []):
                    rows.append(
                        {
                            "object_name": obj_name,
                            "category": cat,
                            "attr_name": res.get("name", ""),
                            "attr_synonym": res.get("synonym", ""),
                            "attr_type": _make_type(res.get("type", "")),
                            "attr_kind": "resource",
                            "ts_name": None,
                            "source_file": resolved,
                        }
                    )
                for ts in parsed.get("tabular_sections", []):
                    ts_name = ts.get("name", "")
                    for ta in ts.get("attributes", []):
                        rows.append(
                            {
                                "object_name": obj_name,
                                "category": cat,
                                "attr_name": ta.get("name", ""),
                                "attr_synonym": ta.get("synonym", ""),
                                "attr_type": _make_type(ta.get("type", "")),
                                "attr_kind": "ts_attribute",
                                "ts_name": ts_name,
                                "source_file": resolved,
                            }
                        )

                if rows:
                    _ext_attrs_cache[key] = rows

            _ext_attrs_cache_built[0] = True

    def _build_ext_predefined_cache() -> None:
        """Parse predefined items from ext objects once per session — mirror
        of ``_build_ext_attrs_cache`` for predefined data.
        """
        if _ext_predefined_cache_built[0]:
            return
        with _ext_predefined_cache_lock:
            if _ext_predefined_cache_built[0]:
                return
            if not _extension_metadata_xml:
                _ext_predefined_cache_built[0] = True
                return

            from rlm_tools_bsl.bsl_xml_parsers import parse_predefined_items as _ppi

            seen: set[tuple[str, str]] = set()
            for cat, obj_name, _rel in _extension_metadata_xml:
                if cat not in _PREDEFINED_CATS:
                    continue
                key = (cat.lower(), obj_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                object_path = f"{cat}/{obj_name}"
                # Use _predefined_candidates to find the predefined.xml/mdo path.
                candidates = _predefined_candidates(object_path)
                content: str | None = None
                source_path: str | None = None
                for p in candidates:
                    try:
                        if not _ext_resolve_safe(p).exists():
                            continue
                    except Exception:
                        continue
                    try:
                        content = _ext_read_file(p)
                        source_path = p
                        break
                    except Exception:
                        continue
                if not content:
                    continue
                items = _ppi(content)
                if not items:
                    continue

                rows: list[dict] = []
                for item in items:
                    rows.append(
                        {
                            "object_name": obj_name,
                            "category": cat,
                            "item_name": item.get("name", ""),
                            "item_synonym": item.get("synonym", ""),
                            "types": item.get("types", []),
                            "item_code": item.get("code", ""),
                            "is_folder": item.get("is_folder", False),
                            "source_file": source_path,
                        }
                    )
                if rows:
                    _ext_predefined_cache[key] = rows

            _ext_predefined_cache_built[0] = True

    def _live_attributes_in_extensions(name: str, category: str, kind: str, limit: int) -> list[dict]:
        """Return ext-side attribute rows matching the name-only query.

        Reads from the per-session ``_ext_attrs_cache`` (built lazily on first
        call). Cold call parses every attribute-bearing ext object's XML once;
        subsequent calls filter the cache in memory. ``limit`` is a soft hint —
        full result returned, caller rank-merges and slices.
        """
        if not _extension_metadata_xml:
            return []
        _build_ext_attrs_cache()

        name_lower = name.lower() if name else ""
        category_lower = category.lower() if category else ""
        kind_lower = kind.lower() if kind else ""
        out: list[dict] = []
        for (cat_lower, _obj_lower), rows in _ext_attrs_cache.items():
            if category_lower and category_lower != cat_lower:
                continue
            for row in rows:
                if kind_lower and kind_lower != row["attr_kind"]:
                    continue
                if name_lower:
                    if name_lower not in row["attr_name"].lower() and name_lower not in row["attr_synonym"].lower():
                        continue
                out.append(row)
        return out

    def _live_predefined_in_extensions(name: str, limit: int) -> list[dict]:
        """Return ext-side predefined items matching the name-only query.

        Mirrors ``_live_attributes_in_extensions`` — reads from per-session
        ``_ext_predefined_cache`` (built lazily on first call).
        """
        if not _extension_metadata_xml:
            return []
        _build_ext_predefined_cache()

        name_lower = name.lower() if name else ""
        out: list[dict] = []
        for rows in _ext_predefined_cache.values():
            for row in rows:
                if name_lower:
                    if name_lower not in row["item_name"].lower() and name_lower not in row["item_synonym"].lower():
                        continue
                out.append(row)
        return out

    def _resolve_object_name_from_extension_metadata(object_name: str) -> tuple[str, str] | None:
        """For bare ``object_name`` (no category prefix, not present as a .bsl
        module), look up an XML-only extension object via
        ``_extension_metadata_xml``. Returns ``(category, "Category/Name")``
        using the CANONICAL ``object_name`` from the metadata entry — so that
        case-mismatch between the user argument and ext-metadata still produces
        a path that matches ``_xml_candidates`` later. Returns ``None`` if no
        ext object matches.
        """
        if not _extension_metadata_xml or not object_name:
            return None
        target = object_name.lower()
        for cat, obj_name, _rel in _extension_metadata_xml:
            if obj_name.lower() == target:
                return cat, f"{cat}/{obj_name}"
        return None

    def find_attributes(
        name: str = "", object_name: str = "", category: str = "", kind: str = "", limit: int = 500
    ) -> list[dict]:
        """Find object attributes/dimensions/resources by name, object, category, or kind."""
        if kind:
            kind = kind.lower()
        if object_name:
            object_name = _strip_meta_prefix(object_name)

        # Build extension state lazily when extensions are configured — the
        # ext attribute/predefined live-fallbacks depend on _extension_metadata_xml.
        if _ext_roots_resolved:
            _ensure_index()

        has_path = object_name and "/" in object_name

        # Fast path: index (None = table missing, [] = authoritative for name-only)
        if idx_reader is not None:
            results = idx_reader.get_object_attributes(
                attr_name=name,
                object_name=object_name,
                category=category,
                kind=kind,
                limit=limit,
            )
            if results is not None:
                if results:  # non-empty — authoritative for main config
                    # Merge ext-side rows for name-only queries BEFORE truncation
                    # (codex round 5): rank-merge by attr_name so ext exact hits
                    # are not starved when main saturates `limit`.
                    if not object_name and _extension_metadata_xml:
                        ext_rows = _live_attributes_in_extensions(name, category, kind, limit)
                        return _rank_merge_ext_into_main(
                            results,
                            ext_rows,
                            name,
                            name_keys=("attr_name", "attr_synonym"),
                            dedup_keys=("category", "object_name", "attr_name", "attr_kind"),
                            limit=limit,
                        )
                    return results[:limit]
                if not object_name:
                    # Name-only search: index returned []. Before declaring authoritative,
                    # let extensions (which are NEVER in the main index) contribute.
                    if _extension_metadata_xml:
                        ext_rows = _live_attributes_in_extensions(name, category, kind, limit)
                        if ext_rows:
                            return _rank_merge_ext_into_main(
                                [],
                                ext_rows,
                                name,
                                name_keys=("attr_name", "attr_synonym"),
                                dedup_keys=("category", "object_name", "attr_name", "attr_kind"),
                                limit=limit,
                            )
                    return results
                # object_name given but empty result — try auto-resolve below

        # Auto-resolve category via find_module (same pattern as analyze_object)
        if object_name and not has_path:
            modules = find_module(object_name)
            exact = [m for m in modules if (m.get("object_name") or "").lower() == object_name.lower()]
            if exact:
                cat = exact[0].get("category", "")
                if cat:
                    object_name = f"{cat}/{object_name}"
                    has_path = True

        # Auto-resolve via extension metadata for XML-only ext objects (no .bsl).
        if object_name and not has_path:
            ext_resolved = _resolve_object_name_from_extension_metadata(object_name)
            if ext_resolved is not None:
                object_name = ext_resolved[1]
                has_path = True

        # Fallback: live XML parse via _resolve_object_xml (same as parse_object_xml)
        if has_path:
            from rlm_tools_bsl.bsl_xml_parsers import normalize_type_string as _nts

            try:
                resolved = _resolve_object_xml(object_name)
                content = _ext_read_file(resolved)
                parsed = parse_metadata_xml(content)
            except Exception:
                return []
            if not parsed:
                return []

            def _make_type(raw: str) -> list[str]:
                import json as _json

                return _json.loads(_nts(raw))

            results = []
            obj_short = object_name.split("/")[-1]
            cat = object_name.split("/")[0]

            # Validate category if provided
            if category and category.lower() != cat.lower():
                return []

            for attr in parsed.get("attributes", []):
                if name and (
                    name.lower() not in attr.get("name", "").lower()
                    and name.lower() not in attr.get("synonym", "").lower()
                ):
                    continue
                if kind and kind != "attribute":
                    continue
                results.append(
                    {
                        "object_name": obj_short,
                        "category": cat,
                        "attr_name": attr.get("name", ""),
                        "attr_synonym": attr.get("synonym", ""),
                        "attr_type": _make_type(attr.get("type", "")),
                        "attr_kind": "attribute",
                        "ts_name": None,
                        "source_file": resolved,
                    }
                )
            for dim in parsed.get("dimensions", []):
                if name and (
                    name.lower() not in dim.get("name", "").lower()
                    and name.lower() not in dim.get("synonym", "").lower()
                ):
                    continue
                if kind and kind != "dimension":
                    continue
                results.append(
                    {
                        "object_name": obj_short,
                        "category": cat,
                        "attr_name": dim.get("name", ""),
                        "attr_synonym": dim.get("synonym", ""),
                        "attr_type": _make_type(dim.get("type", "")),
                        "attr_kind": "dimension",
                        "ts_name": None,
                        "source_file": resolved,
                    }
                )
            for res in parsed.get("resources", []):
                if name and (
                    name.lower() not in res.get("name", "").lower()
                    and name.lower() not in res.get("synonym", "").lower()
                ):
                    continue
                if kind and kind != "resource":
                    continue
                results.append(
                    {
                        "object_name": obj_short,
                        "category": cat,
                        "attr_name": res.get("name", ""),
                        "attr_synonym": res.get("synonym", ""),
                        "attr_type": _make_type(res.get("type", "")),
                        "attr_kind": "resource",
                        "ts_name": None,
                        "source_file": resolved,
                    }
                )
            for ts in parsed.get("tabular_sections", []):
                for ta in ts.get("attributes", []):
                    if name and (
                        name.lower() not in ta.get("name", "").lower()
                        and name.lower() not in ta.get("synonym", "").lower()
                    ):
                        continue
                    if kind and kind != "ts_attribute":
                        continue
                    results.append(
                        {
                            "object_name": obj_short,
                            "category": cat,
                            "attr_name": ta.get("name", ""),
                            "attr_synonym": ta.get("synonym", ""),
                            "attr_type": _make_type(ta.get("type", "")),
                            "attr_kind": "ts_attribute",
                            "ts_name": ts.get("name", ""),
                            "source_file": resolved,
                        }
                    )
            return results[:limit]

        # No idx_reader, no object_name → scan extension metadata as the only live source.
        if _extension_metadata_xml and not object_name:
            ext_rows = _live_attributes_in_extensions(name, category, kind, limit)
            return _rank_merge_ext_into_main(
                [],
                ext_rows,
                name,
                name_keys=("attr_name", "attr_synonym"),
                dedup_keys=("category", "object_name", "attr_name", "attr_kind"),
                limit=limit,
            )
        return []

    def _predefined_candidates(object_name: str) -> list[str]:
        """Predefined.xml/MDO path candidates for ``Category/Name``.

        EDT keeps predefined items inside the object's ``.mdo`` file; CF uses a
        separate ``Ext/Predefined.xml``. Extension layouts mirror these.
        """
        parts = object_name.split("/")
        category = parts[0].lower() if parts else ""
        obj_short = parts[-1] if parts else ""

        candidates: list[str] = []
        if obj_short:
            candidates.append(f"{object_name}/Ext/Predefined.xml")
            candidates.append(f"{object_name}/{obj_short}.mdo")

        # Extension candidates from the metadata-XML pass.
        if _extension_metadata_xml and category and obj_short:
            target_cat = category.lower()
            target_name = obj_short.lower()
            for cat, ent_name, rel in _extension_metadata_xml:
                if cat.lower() != target_cat or ent_name.lower() != target_name:
                    continue
                if rel.endswith(".mdo"):
                    candidates.append(rel)
                else:
                    # Derive the object dir from the locator, which may be either
                    # a sibling Cat/Name.xml (real CF/CFE dump) or Cat/Name/Ext/<Type>.xml.
                    rel_p = rel.replace("\\", "/")
                    parent = os.path.dirname(rel_p)
                    if parent.endswith("/Ext"):
                        ext_obj_dir = parent[: -len("/Ext")]
                    elif rel_p.lower().endswith(".xml"):
                        ext_obj_dir = rel_p[:-4]  # strip ".xml" → object dir
                    else:
                        ext_obj_dir = ""
                    if ext_obj_dir:
                        candidates.append(f"{ext_obj_dir}/Ext/Predefined.xml")
        return candidates

    def find_predefined(name: str = "", object_name: str = "", limit: int = 500) -> list[dict]:
        """Find predefined items of ChartsOfCharacteristicTypes, Catalogs, ChartsOfAccounts."""
        if object_name:
            object_name = _strip_meta_prefix(object_name)
        if _ext_roots_resolved:
            _ensure_index()
        has_path = object_name and "/" in object_name

        # Fast path: index (None = table missing, [] = authoritative for name-only)
        if idx_reader is not None:
            results = idx_reader.get_predefined_items(item_name=name, object_name=object_name, limit=limit)
            if results is not None:
                if results:  # non-empty — authoritative for main config
                    # Merge ext rows BEFORE truncation (codex round 5).
                    if not object_name and _extension_metadata_xml:
                        ext_rows = _live_predefined_in_extensions(name, limit)
                        return _rank_merge_ext_into_main(
                            results,
                            ext_rows,
                            name,
                            name_keys=("item_name", "item_synonym"),
                            dedup_keys=("category", "object_name", "item_name"),
                            limit=limit,
                        )
                    return results[:limit]
                if not object_name:
                    # Name-only search: index returned []. Let extensions contribute.
                    if _extension_metadata_xml:
                        ext_rows = _live_predefined_in_extensions(name, limit)
                        if ext_rows:
                            return _rank_merge_ext_into_main(
                                [],
                                ext_rows,
                                name,
                                name_keys=("item_name", "item_synonym"),
                                dedup_keys=("category", "object_name", "item_name"),
                                limit=limit,
                            )
                    return results
                # object_name given but empty result — try auto-resolve below

        # Index-authoritative for name-only search (no live XML scan across 6820+ files);
        # extensions are NEVER in the main index, so let them contribute live (v1.12.0).
        if not object_name:
            if _extension_metadata_xml:
                ext_rows = _live_predefined_in_extensions(name, limit)
                if ext_rows:
                    return _rank_merge_ext_into_main(
                        [],
                        ext_rows,
                        name,
                        name_keys=("item_name", "item_synonym"),
                        dedup_keys=("category", "object_name", "item_name"),
                        limit=limit,
                    )
            return []

        # Auto-resolve category via find_module (same pattern as analyze_object)
        if not has_path:
            modules = find_module(object_name)
            exact = [m for m in modules if (m.get("object_name") or "").lower() == object_name.lower()]
            if exact:
                cat = exact[0].get("category", "")
                if cat:
                    object_name = f"{cat}/{object_name}"
                    has_path = True

        # Auto-resolve via extension metadata for XML-only ext objects (no .bsl).
        if not has_path:
            ext_resolved = _resolve_object_name_from_extension_metadata(object_name)
            if ext_resolved is not None:
                object_name = ext_resolved[1]
                has_path = True

        if not has_path:
            return []

        from rlm_tools_bsl.bsl_xml_parsers import parse_predefined_items as _ppi

        obj_short = object_name.split("/")[-1]
        candidates = _predefined_candidates(object_name)

        for p in candidates:
            try:
                if not _ext_resolve_safe(p).exists():
                    continue
            except Exception:
                continue
            try:
                content = _ext_read_file(p)
            except Exception:
                continue
            items = _ppi(content)
            if not items:
                continue
            results = []
            for item in items:
                if (
                    name
                    and name.lower() not in item["name"].lower()
                    and name.lower() not in item.get("synonym", "").lower()
                ):
                    continue
                results.append(
                    {
                        "object_name": obj_short,
                        "category": object_name.split("/")[0] if "/" in object_name else "",
                        "item_name": item["name"],
                        "item_synonym": item.get("synonym", ""),
                        "types": item.get("types", []),
                        "item_code": item.get("code", ""),
                        "is_folder": item.get("is_folder", False),
                        "source_file": p,
                    }
                )
            return results[:limit]

        return []

    _fo_lazy = LazyList()

    def _build_functional_options() -> list[dict]:
        files = glob_files_fn("**/FunctionalOptions/**/*.xml")
        files.extend(glob_files_fn("**/FunctionalOptions/**/*.mdo"))
        files.extend(glob_files_fn("**/FunctionalOptions/*.xml"))
        files.extend(glob_files_fn("**/FunctionalOptions/*.mdo"))
        files = list(dict.fromkeys(files))
        result: list[dict] = []
        for f in files:
            try:
                content = read_file_fn(f)
            except Exception:
                continue
            parsed = parse_functional_option_xml(content)
            if parsed is None:
                continue
            parsed["file"] = f
            result.append(parsed)
        return result

    def _ensure_functional_options() -> list[dict]:
        return _fo_lazy.ensure(_build_functional_options)

    def find_functional_options(object_name: str) -> dict:
        """Find functional options that affect a given object.
        Also greps BSL modules for ПолучитьФункциональнуюОпцию("X") pattern.
        Uses SQLite index for XML options when available.

        Args:
            object_name: Object name to search for in FO content lists.

        Returns: dict with object, xml_options, code_options."""
        object_name = _strip_meta_prefix(object_name)

        # --- Fast path for xml_options: SQLite index ---
        xml_options: list[dict] | None = None
        if idx_reader is not None:
            xml_options = idx_reader.get_functional_options(object_name)

        if xml_options is None:
            all_fo = _ensure_functional_options()
            name_lower = object_name.lower()
            xml_options = []
            for fo in all_fo:
                matched = any(name_lower in c.lower() for c in fo.get("content", []))
                if matched:
                    xml_options.append(dict(fo))

        # Grep for ПолучитьФункциональнуюОпцию in BSL code
        code_options: list[dict] = []
        try:
            grep_results = safe_grep("ПолучитьФункциональнуюОпцию", name_hint=object_name)
            for r in grep_results:
                text = r.get("text", "") or r.get("content", "")
                # Extract option name from ПолучитьФункциональнуюОпцию("OptionName")
                m = re.search(r'ПолучитьФункциональнуюОпцию\(\s*"([^"]+)"', text)
                if m:
                    code_options.append(
                        {
                            "option_name": m.group(1),
                            "file": r.get("file", ""),
                            "line": r.get("line", 0),
                        }
                    )
        except Exception:
            pass

        return {
            "object": object_name,
            "xml_options": xml_options,
            "code_options": code_options,
        }

    def find_roles(object_name: str) -> dict:
        """Find roles that grant rights to a given object.

        Args:
            object_name: Object name substring to filter rights by.

        Returns: dict with object, roles list."""
        object_name = _strip_meta_prefix(object_name)

        # Fast path: SQLite index
        if idx_reader is not None:
            idx_roles = idx_reader.get_roles(object_name)
            if idx_roles is not None:
                return {"object": object_name, "roles": idx_roles}

        # Fallback: glob + XML parse
        patterns = [
            "**/Roles/*/Ext/Rights.xml",
            "**/Roles/*/*.rights",
        ]
        found_files: list[str] = []
        for p in patterns:
            found_files.extend(glob_files_fn(p))
        found_files = list(dict.fromkeys(found_files))

        roles: list[dict] = []
        for f in found_files:
            # Extract role name from path: Roles/RoleName/Ext/Rights.xml
            parts = f.replace("\\", "/").split("/")
            role_name = ""
            for i, part in enumerate(parts):
                if part == "Roles" and i + 1 < len(parts):
                    role_name = parts[i + 1]
                    break

            try:
                content = read_file_fn(f)
            except Exception:
                continue
            rights = parse_rights_xml(content, object_name)
            for r in rights:
                roles.append(
                    {
                        "role_name": role_name,
                        "object": r["object"],
                        "rights": r["rights"],
                        "file": f,
                    }
                )

        # Group by role_name, merge rights (match index behavior)
        grouped: dict[str, dict] = {}
        for r in roles:
            key = r["role_name"]
            if key not in grouped:
                grouped[key] = {
                    "role_name": key,
                    "object": object_name,
                    "rights": [],
                    "file": r["file"],
                }
            for right in r["rights"]:
                if right not in grouped[key]["rights"]:
                    grouped[key]["rights"].append(right)

        return {"object": object_name, "roles": list(grouped.values())}

    # ── FTS search (requires SQLite index with FTS5) ────────────

    def _iter_extension_bsl() -> list[tuple[str, BslFileInfo]]:
        """Return only the extension-side rows from ``_index_state``."""
        if not _extension_paths_set:
            return []
        return [(rel, info) for rel, info in _index_state if rel in _extension_paths_set]

    def _rank_merge_ext_into_main(
        main_rows: list[dict],
        ext_rows: list[dict],
        query: str,
        name_keys: tuple[str, ...],
        dedup_keys: tuple[str, ...],
        limit: int,
    ) -> list[dict]:
        """Merge main+ext rows with 3-level rank applied to ext, dedup by
        ``dedup_keys``, slice to ``limit``.

        ``name_keys`` is a tuple of fields to rank against — the row's rank is
        the BEST (lowest) rank found across all listed fields. This mirrors
        ``IndexReader.get_object_attributes`` / ``get_predefined_items`` which
        match against ``attr_name OR attr_synonym`` (resp. ``item_name OR
        item_synonym``), so passing both keys lets a row matching by Russian
        synonym ALSO claim rank 0/1 instead of being silently rank 2 and
        sliced away by a saturated main result (codex round 6).

        Strategy: ext rows with rank 0 (exact match on any key) or 1 (prefix
        on any key) go BEFORE all main rows. Main rows keep their original
        ordering (FTS or index-native). Ext rows with rank 2 (substring only)
        go AFTER main rows.
        """
        if not ext_rows:
            return list(main_rows)[:limit]
        seen = {tuple((r.get(k) or "") for k in dedup_keys) for r in main_rows}
        ext_dedup = [r for r in ext_rows if tuple((r.get(k) or "") for k in dedup_keys) not in seen]
        if not ext_dedup:
            return list(main_rows)[:limit]
        q_lower = (query or "").lower()
        if not q_lower:
            return (list(main_rows) + ext_dedup)[:limit]

        def _rank(row: dict) -> int:
            best = 2
            for key in name_keys:
                n = (row.get(key) or "").lower()
                if n == q_lower:
                    return 0
                if n.startswith(q_lower):
                    if best > 1:
                        best = 1
            return best

        primary_key = name_keys[0]
        ext_top = sorted(
            (r for r in ext_dedup if _rank(r) < 2),
            key=lambda r: (_rank(r), (r.get(primary_key) or "").lower()),
        )
        ext_bottom = [r for r in ext_dedup if _rank(r) >= 2]
        merged = ext_top + list(main_rows) + ext_bottom
        return merged[:limit]

    def _reserve_merge_ext_into_main(
        main_rows: list[dict],
        ext_rows: list[dict],
        dedup_keys: tuple[str, ...],
        limit: int,
        quota_ratio: int = 5,
    ) -> list[dict]:
        """Merge for helpers without a meaningful name-based rank
        (e.g. search_module_headers). Reserves up to
        ``min(len(ext), max(1, limit // quota_ratio))`` slots for ext rows by
        clipping the main tail, so a saturated main result still surfaces
        extension hits.
        """
        if not ext_rows:
            return list(main_rows)[:limit]
        seen = {tuple((r.get(k) or "") for k in dedup_keys) for r in main_rows}
        ext_dedup = [r for r in ext_rows if tuple((r.get(k) or "") for k in dedup_keys) not in seen]
        if not ext_dedup:
            return list(main_rows)[:limit]
        quota = min(len(ext_dedup), max(1, limit // quota_ratio))
        main_keep = max(0, limit - quota)
        return (list(main_rows)[:main_keep] + ext_dedup[:quota])[:limit]

    def _live_search_methods(query: str, limit: int) -> list[dict]:
        """Substring search in extension .bsl procedures.

        Result shape matches ``IndexReader.search_methods`` exactly, including
        ``rank=None`` (FTS-bm25 cannot be reproduced live). Full scan with no
        early break — the caller (``search_methods``) rank-merges and slices
        last so a high-quality ext hit (exact name) is not lost when main FTS
        already returned `limit` rows.
        """
        if not _extension_paths_set or not query:
            return []
        needle = query.lower()
        out: list[dict] = []
        for rel, info in _iter_extension_bsl():
            try:
                procs = _parse_procedures(rel)
            except Exception:
                continue
            for proc in procs:
                if needle not in proc["name"].lower():
                    continue
                out.append(
                    {
                        "name": proc["name"],
                        "type": proc["type"],
                        "is_export": proc["is_export"],
                        "line": proc["line"],
                        "end_line": proc["end_line"],
                        "params": proc["params"],
                        "module_path": rel,
                        "object_name": info.object_name,
                        "rank": None,
                    }
                )
        return out

    def _live_search_objects(query: str, limit: int) -> list[dict]:
        """Substring search in extension object synonyms / object names.

        Empty/whitespace ``query`` → alphabetical listing sorted by
        ``(category, object_name)``, sliced to ``limit`` (mirrors
        ``IndexReader.search_objects("")``).

        Non-empty query → **full scan**, no early slice. Mirrors the indexer's
        contract: ``IndexReader.search_objects`` explicitly does NOT apply a
        SQL LIMIT for substring queries because Python-side 4-level ranking
        needs all matches to guarantee an exact-name hit is never lost. The
        caller (``search_objects``) re-ranks the merged list and slices last.
        """
        if not _extension_synonyms:
            return []
        _ensure_index()

        if not query or not query.strip():
            rows = [
                {
                    "object_name": obj_name,
                    "category": cat,
                    "synonym": prefixed_synonym,
                    "file": rel,
                }
                for obj_name, cat, prefixed_synonym, rel in _extension_synonyms
            ]
            rows.sort(key=lambda r: (r["category"], r["object_name"]))
            return rows[:limit]

        needle = query.lower()
        out: list[dict] = []
        for obj_name, cat, prefixed_synonym, rel in _extension_synonyms:
            if needle in prefixed_synonym.lower() or needle in obj_name.lower():
                out.append(
                    {
                        "object_name": obj_name,
                        "category": cat,
                        "synonym": prefixed_synonym,
                        "file": rel,
                    }
                )
        return out

    def _live_search_regions(query: str, limit: int) -> list[dict]:
        """Substring search over #Область / #Region declarations in extension .bsl.

        Full scan, no early break — caller rank-merges and slices last.
        """
        if not _extension_paths_set or not query:
            return []
        needle = query.lower()
        region_start = re.compile(BSL_PATTERNS["region_start"], re.IGNORECASE)
        region_end = re.compile(BSL_PATTERNS["region_end"], re.IGNORECASE)
        out: list[dict] = []
        for rel, info in _iter_extension_bsl():
            try:
                content = _ext_read_file(rel)
            except Exception:
                continue
            lines = content.splitlines()
            open_stack: list[tuple[str, int]] = []
            for line_idx, line in enumerate(lines, 1):
                m_start = region_start.search(line)
                if m_start:
                    open_stack.append((m_start.group(1), line_idx))
                    continue
                if region_end.search(line) and open_stack:
                    name, start = open_stack.pop()
                    if needle in name.lower():
                        out.append(
                            {
                                "name": name,
                                "line": start,
                                "end_line": line_idx,
                                "module_path": rel,
                                "object_name": info.object_name,
                                "category": info.category,
                            }
                        )
            # Unclosed regions at EOF: skip — same behavior as indexer.
        return out

    def _live_search_module_headers(query: str, limit: int) -> list[dict]:
        """Substring search over leading-comment blocks in extension .bsl.

        Full scan, no early break — caller reserves a quota and slices last.
        """
        if not _extension_paths_set or not query:
            return []
        needle = query.lower()
        out: list[dict] = []
        for rel, info in _iter_extension_bsl():
            try:
                content = _ext_read_file(rel)
            except Exception:
                continue
            lines = content.splitlines()[:30]
            header_lines: list[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("//"):
                    header_lines.append(stripped[2:].strip())
                elif stripped == "":
                    if header_lines:
                        continue
                else:
                    break
            header_comment = "\n".join(header_lines).strip()
            if not header_comment or needle not in header_comment.lower():
                continue
            out.append(
                {
                    "module_path": rel,
                    "object_name": info.object_name,
                    "category": info.category,
                    "header_comment": header_comment,
                }
            )
        return out

    def search_methods(query: str, limit: int = 30) -> list[dict]:
        """Full-text search for methods by name substring (FTS5 trigram).
        Requires a pre-built SQLite index with FTS enabled.

        Args:
            query: Search substring (e.g. 'Провед', 'ОбработкаЗаполнения').
            limit: Max results (default 30).

        Returns: list of dicts {name, type, is_export, line, end_line, params,
                 module_path, object_name, rank} ordered by relevance.
                 Empty list if index/FTS not available."""
        result: list[dict] = []
        if idx_reader is not None and idx_reader.has_fts:
            result = list(idx_reader.search_methods(query, limit))
        _ensure_index()
        # Merge BEFORE truncation: even when main FTS fills `limit`, an ext
        # method with exact-name match must be visible. Rank-merge with 3-level
        # scheme on `name`: rank 0 (exact) and 1 (prefix) ext rows go BEFORE
        # main; rank 2 (substring) ext rows go AFTER main, sliced last (codex
        # round 5).
        if _extension_paths_set and query:
            ext_rows = _live_search_methods(query, limit)
            result = _rank_merge_ext_into_main(
                result, ext_rows, query, name_keys=("name",), dedup_keys=("module_path", "name"), limit=limit
            )
        return result[:limit]

    def search_objects(query: str = "", limit: int = 50) -> list[dict]:
        """Search 1C objects by business name (Russian synonym) or technical name.
        Uses pre-built SQLite index with object synonyms.

        Args:
            query: Search string (e.g. 'себестоимость', 'Авансовый', 'общий модуль').
            limit: Max results (default 50).

        Returns: list of dicts {object_name, category, synonym, file}.
                 Empty list if index not available or no synonyms built."""
        result: list[dict] = []
        if idx_reader is not None:
            indexed = idx_reader.search_objects(query, limit)
            if indexed is not None:
                result = list(indexed)
        _ensure_index()
        # Extension synonyms are never in the main index; merge BEFORE truncation
        # so a saturated main result does not starve ext rows. Empty query →
        # re-sort the merged list alphabetically by (category, object_name) to
        # honour IndexReader.search_objects("") contract. Non-empty query →
        # re-rank using the same 4-level scheme IndexReader uses (exact name >
        # prefix > synonym substring > category), so a matching ext object wins
        # a slot from a low-rank main row instead of being sliced away at
        # position 51+ (v1.12.0; codex round 3).
        if _extension_synonyms:
            seen = {(r.get("file", ""), r.get("object_name", "")) for r in result}
            ext_rows = [
                row for row in _live_search_objects(query, limit) if (row["file"], row["object_name"]) not in seen
            ]
            if ext_rows:
                is_empty_query = not query or not query.strip()
                if is_empty_query:
                    merged = result + ext_rows
                    merged.sort(key=lambda r: (r.get("category", ""), r.get("object_name", "")))
                    result = merged
                else:
                    q_lower = query.strip().lower()

                    def _rank_for_query(row: dict) -> int:
                        # Mirrors IndexReader.search_objects ranking exactly.
                        name_lower = (row.get("object_name") or "").lower()
                        synonym_lower = (row.get("synonym") or "").lower()
                        if name_lower == q_lower:
                            return 0
                        if name_lower.startswith(q_lower):
                            return 1
                        synonym_tail = synonym_lower.split(": ", 1)[-1] if ": " in synonym_lower else synonym_lower
                        if q_lower in synonym_tail:
                            return 2
                        return 3

                    ranked = [
                        (_rank_for_query(r), r.get("category", ""), r.get("object_name", ""), r)
                        for r in result + ext_rows
                    ]
                    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
                    result = [item[3] for item in ranked]
        return result[:limit]

    def search_regions(query: str = "", limit: int = 200) -> list[dict]:
        """Search code regions (#Область/#Region) by name substring.

        Args:
            query: Search string (e.g. 'Себестоимость', 'Инициализация').
            limit: Max results (default 200).

        Returns: list of dicts {name, line, end_line, module_path, object_name, category}.
                 Empty list if index not available or no regions built."""
        result: list[dict] = []
        if idx_reader is not None:
            indexed = idx_reader.search_regions(query, limit)
            if indexed is not None:
                result = list(indexed)
        _ensure_index()
        # Same rank-merge as search_methods — see _rank_merge_ext_into_main.
        if _extension_paths_set and query:
            ext_rows = _live_search_regions(query, limit)
            result = _rank_merge_ext_into_main(
                result, ext_rows, query, name_keys=("name",), dedup_keys=("module_path", "name"), limit=limit
            )
        return result[:limit]

    def search_module_headers(query: str = "", limit: int = 200) -> list[dict]:
        """Search module header comments by substring.

        Args:
            query: Search string (e.g. 'себестоимость', 'доработка').
            limit: Max results (default 200).

        Returns: list of dicts {module_path, object_name, category, header_comment}.
                 Empty list if index not available or no headers built."""
        result: list[dict] = []
        if idx_reader is not None:
            indexed = idx_reader.search_module_headers(query, limit)
            if indexed is not None:
                result = list(indexed)
        _ensure_index()
        # No clear name field for rank → reserve a quota for ext rows so a
        # saturated main index does not starve them (codex round 5).
        if _extension_paths_set and query:
            ext_rows = _live_search_module_headers(query, limit)
            result = _reserve_merge_ext_into_main(
                result, ext_rows, dedup_keys=("module_path", "header_comment"), limit=limit
            )
        return result[:limit]

    _VALID_SCOPES = frozenset({"all", "methods", "objects", "regions", "headers", "attributes", "predefined"})

    def search(query: str, scope: str = "all", limit: int = 30) -> list[dict]:
        """Unified search across methods, objects, regions, headers, attributes, predefined.

        Args:
            query: Search string (required).
            scope: Filter — 'all', 'methods', 'objects', 'regions', 'headers', 'attributes', 'predefined'.
            limit: Max results (applied to final list).

        Returns: list of dicts {text, source_type, object_name, path, path_kind, detail}.
        """
        if scope not in _VALID_SCOPES:
            msg = f"Unknown scope '{scope}'. Valid: {', '.join(sorted(_VALID_SCOPES))}"
            raise ValueError(msg)

        query = query.strip() if query else ""
        empty_query = not query
        if empty_query and scope == "all":
            return []

        per_source = max(limit // 6, 3) if scope == "all" else limit
        results: list[dict] = []

        if scope in ("all", "methods"):
            if not empty_query:  # search_methods('') → [] by design
                for m in search_methods(query, limit=per_source):
                    results.append(
                        {
                            "text": m["name"],
                            "source_type": "method",
                            "object_name": m.get("object_name", ""),
                            "path": m.get("module_path", ""),
                            "path_kind": "bsl",
                            "detail": m,
                        }
                    )

        if scope in ("all", "objects"):
            raw = search_objects(query, limit=per_source)
            if raw:
                for o in raw:
                    results.append(
                        {
                            "text": o["synonym"],
                            "source_type": "object",
                            "object_name": o.get("object_name", ""),
                            "path": o.get("file", ""),
                            "path_kind": "metadata",
                            "detail": o,
                        }
                    )

        if scope in ("all", "regions"):
            raw = search_regions(query, limit=per_source)
            if raw:
                for r in raw:
                    results.append(
                        {
                            "text": r["name"],
                            "source_type": "region",
                            "object_name": r.get("object_name", ""),
                            "path": r.get("module_path", ""),
                            "path_kind": "bsl",
                            "detail": r,
                        }
                    )

        if scope in ("all", "headers"):
            raw = search_module_headers(query, limit=per_source)
            if raw:
                for h in raw:
                    results.append(
                        {
                            "text": h["header_comment"],
                            "source_type": "header",
                            "object_name": h.get("object_name", ""),
                            "path": h.get("module_path", ""),
                            "path_kind": "bsl",
                            "detail": h,
                        }
                    )

        if scope in ("all", "attributes"):
            _attrs = find_attributes(name=query) if query else find_attributes()
            for a in _attrs[:per_source]:
                type_str = ", ".join(a["attr_type"]) if a["attr_type"] else ""
                results.append(
                    {
                        "text": f"{a['attr_name']} ({type_str})" if type_str else a["attr_name"],
                        "source_type": "attribute",
                        "object_name": a.get("object_name", ""),
                        "path": a.get("source_file", ""),
                        "path_kind": "metadata",
                        "detail": a,
                    }
                )

        if scope in ("all", "predefined"):
            _preds = find_predefined(name=query) if query else find_predefined()
            for p in _preds[:per_source]:
                type_str = ", ".join(p["types"]) if p.get("types") else ""
                results.append(
                    {
                        "text": f"{p.get('item_synonym') or p['item_name']} ({type_str})"
                        if type_str
                        else p.get("item_synonym") or p["item_name"],
                        "source_type": "predefined",
                        "object_name": p.get("object_name", ""),
                        "path": p.get("source_file", ""),
                        "path_kind": "metadata",
                        "detail": p,
                    }
                )

        return results[:limit]

    def get_index_info() -> dict:
        """Return index metadata: version, capabilities, staleness."""
        if idx_reader is None:
            return {"status": "no_index"}
        stats = idx_reader.get_statistics()
        builder = int(stats.get("builder_version") or 0)
        return {
            "status": "ok",
            "builder_version": builder,
            "config_name": stats.get("config_name", ""),
            "config_version": stats.get("config_version", ""),
            "modules": stats.get("modules", 0),
            "methods": stats.get("methods", 0),
            "has_fts": stats.get("has_fts", False),
            "has_synonyms": bool(stats.get("object_synonyms", 0)),
            "object_synonyms": stats.get("object_synonyms", 0),
            "has_regions": builder >= 8,
            "has_module_headers": builder >= 8,
            "has_extension_overrides": builder >= 9,
            "extension_overrides": stats.get("extension_overrides", 0),
            "has_form_elements": builder >= 10 and stats.get("has_metadata", False),
            "form_elements_count": stats.get("form_elements", 0),
            "has_object_attributes": builder >= 11 and stats.get("has_metadata", False),
            "object_attributes_count": stats.get("object_attributes", 0),
            "has_predefined_items": builder >= 11 and stats.get("has_metadata", False),
            "predefined_items_count": stats.get("predefined_items", 0),
            # v12 reverse-index (v1.9.0+): metadata_references + 3 specialised tables
            "has_metadata_references": builder >= 12 and (stats.get("metadata_references") or 0) > 0,
            "metadata_references_count": stats.get("metadata_references", 0),
            "exchange_plan_content_count": stats.get("exchange_plan_content", 0),
            "defined_types_count": stats.get("defined_types", 0),
            "characteristic_types_count": stats.get("characteristic_types", 0),
            # v13 reverse code-usage index (v1.14.0). Capability is builder-gated,
            # NOT count>0 — an empty table is a valid (no-usages) answer.
            "has_metadata_code_usages": builder >= 13,
            "metadata_code_usages_count": stats.get("metadata_code_usages", 0),
            # Git fast-path acceleration availability for incremental update (v1.8.0+)
            "git_accelerated": bool(stats.get("git_accelerated")),
            "git_head_commit": stats.get("git_head_commit"),
            "built_at": stats.get("built_at"),
        }

    # ── Help (uses _registry for recipes) ──────────────────────

    def bsl_help(task: str = "") -> str:
        """Get a recipe for your task. Call help() to see all recipes,
        or help('find exports') / help('граф вызовов') for a specific one.

        Returns: str with Python code example."""
        task_lower = task.lower()

        if not task_lower:
            lines = ["Available recipes (call help('keyword') for details):\n"]
            for name, entry in _registry.items():
                if entry["recipe"]:
                    first_line = entry["recipe"].split("\n")[0]
                    lines.append(f"  help('{name}') - {first_line}")
            return "\n".join(lines)

        # Search by helper name first (exact match)
        if task_lower in _registry and _registry[task_lower]["recipe"]:
            return _registry[task_lower]["recipe"]

        # Pass 1: ТОЧНОЕ совпадение task с keyword.
        # Без этого прохода длинные keywords типа "иерархия вызовов" в
        # find_call_hierarchy теряются: substring "вызов" в kw у
        # find_callers_context (зарегистрирован раньше) ловится первым.
        # Точное совпадение даёт правильный приоритет независимо от порядка
        # регистрации.
        for name, entry in _registry.items():
            if not entry["recipe"]:
                continue
            for kw in entry["kw"]:
                if kw == task_lower:
                    return entry["recipe"]

        # Pass 2: substring matching (для запросов, не совпадающих точно).
        for name, entry in _registry.items():
            if not entry["recipe"]:
                continue
            if name in task_lower:
                return entry["recipe"]
            for kw in entry["kw"]:
                if kw in task_lower:
                    return entry["recipe"]

        # Bridge to _BUSINESS_RECIPES (G.5b) — for words that are recipe domain
        # keys / aliases but not helper keywords.
        try:
            from rlm_tools_bsl.bsl_knowledge import _BUSINESS_RECIPES, _match_recipe

            domain = _match_recipe(task_lower)
            if domain and domain in _BUSINESS_RECIPES:
                recipe = _BUSINESS_RECIPES[domain]
                lines = [f"BUSINESS RECIPE: {domain}", ""]
                for i, step in enumerate(recipe.get("compact", []), 1):
                    lines.append(f"  {i}. {step}")
                code_hint = recipe.get("code_hint")
                if code_hint:
                    lines += ["", "Ready-to-use code:", code_hint]
                return "\n".join(lines)
        except Exception:
            pass  # bridge не должен ломать существующее поведение

        # Fallback: show all recipes
        return bsl_help("")

    # ── Query extraction ───────────────────────────────────────

    _QUERY_ASSIGN_RE = re.compile(
        r'(?:Запрос\.Текст|ТекстЗапроса)\s*=\s*["\']',
        re.IGNORECASE,
    )
    _QUERY_TABLE_RE = re.compile(
        r"\b(?:ИЗ|FROM|СОЕДИНЕНИЕ|JOIN)\s+"
        r"((?:РегистрНакопления|РегистрСведений|РегистрБухгалтерии|"
        r"Справочник|Документ|"
        r"AccumulationRegister|InformationRegister|AccountingRegister|"
        r"Catalog|Document)\.\w+)",
        re.IGNORECASE,
    )

    def extract_queries(path: str) -> list[dict]:
        """Extract embedded 1C queries from a BSL module.

        Finds Запрос.Текст = "..." and ТекстЗапроса = "..." patterns,
        extracts table names from query text.

        Returns: list of dicts {procedure, line, tables: [str], text_preview}."""
        content = _ext_read_file(path)
        lines = content.splitlines()
        procs = extract_procedures(path)

        queries: list[dict] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = _QUERY_ASSIGN_RE.search(line)
            if not m:
                i += 1
                continue

            # Collect multiline query text (1C uses | prefix for continuation)
            query_start = i
            query_lines = [line[m.end() :]]
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                if stripped.startswith("|") or stripped.startswith('"'):
                    query_lines.append(stripped.lstrip("|").lstrip('"'))
                elif stripped.startswith("'") or stripped == "":
                    query_lines.append(stripped.lstrip("'"))
                else:
                    break
                j += 1
            query_text = "\n".join(query_lines)

            # Extract table names
            tables = list(dict.fromkeys(m2.group(1) for m2 in _QUERY_TABLE_RE.finditer(query_text)))

            # Determine which procedure this belongs to
            line_num = query_start + 1  # 1-based
            proc_name = ""
            for p in procs:
                if p["line"] <= line_num <= (p["end_line"] or len(lines)):
                    proc_name = p["name"]
                    break

            preview = query_text[:200].strip()
            if len(query_text) > 200:
                preview += "..."

            queries.append(
                {
                    "procedure": proc_name,
                    "line": line_num,
                    "tables": tables,
                    "text_preview": preview,
                }
            )
            i = j
        return queries

    # ── Code metrics ─────────────────────────────────────────

    _COMMENT_RE = re.compile(r"^\s*//")
    _NESTING_OPEN_RE = re.compile(r"\b(Если|Для|Пока|Попытка|If|For|While|Try)\b", re.IGNORECASE)
    _NESTING_CLOSE_RE = re.compile(r"\b(КонецЕсли|КонецЦикла|КонецПопытки|EndIf|EndDo|EndTry)\b", re.IGNORECASE)

    def code_metrics(path: str) -> dict:
        """Compute code metrics for a BSL module.

        Returns: dict {total_lines, code_lines, comment_lines, empty_lines,
                 procedures_count, exports_count, avg_proc_size, max_nesting}."""
        content = _ext_read_file(path)
        lines = content.splitlines()

        # Single-pass: empty, comment, nesting depth
        total = len(lines)
        empty = 0
        comment = 0
        max_nesting = 0
        current_nesting = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                empty += 1
            elif _COMMENT_RE.match(line):
                comment += 1
            else:
                for _ in _NESTING_OPEN_RE.finditer(line):
                    current_nesting += 1
                    if current_nesting > max_nesting:
                        max_nesting = current_nesting
                for _ in _NESTING_CLOSE_RE.finditer(line):
                    current_nesting = max(0, current_nesting - 1)
        code = total - empty - comment

        procs = extract_procedures(path)
        exports = [p for p in procs if p.get("is_export")]

        sizes = [(p["end_line"] or total) - p["line"] + 1 for p in procs]
        avg_size = round(sum(sizes) / len(sizes), 1) if sizes else 0

        return {
            "total_lines": total,
            "code_lines": code,
            "comment_lines": comment,
            "empty_lines": empty,
            "procedures_count": len(procs),
            "exports_count": len(exports),
            "avg_proc_size": avg_size,
            "max_nesting": max_nesting,
        }

    # ── Extensions ───────────────────────────────────────────

    def detect_extensions() -> dict:
        """Обнаружить расширения рядом и текущую роль конфигурации."""
        from rlm_tools_bsl.extension_detector import detect_extension_context as _det

        ctx = _det(base_path)
        result = {
            "config_role": ctx.current.role.value,
            "config_name": ctx.current.name,
            "config_prefix": ctx.current.name_prefix,
            "warnings": ctx.warnings,
            "nearby_extensions": [
                {"name": e.name, "purpose": e.purpose, "prefix": e.name_prefix, "path": e.path}
                for e in ctx.nearby_extensions
            ],
            "nearby_main": None,
        }
        if ctx.nearby_main:
            result["nearby_main"] = {
                "name": ctx.nearby_main.name,
                "path": ctx.nearby_main.path,
            }
        return result

    def find_ext_overrides(extension_path: str, object_name: str = "") -> dict:
        """Найти перехваченные методы в расширении.
        extension_path — путь к расширению (из detect_extensions).
        object_name — имя объекта для прицельного поиска ('' = все)."""
        from rlm_tools_bsl.extension_detector import find_extension_overrides as _feo

        overrides = _feo(extension_path, object_name or None)
        return {
            "extension_path": extension_path,
            "object_filter": object_name or "(all)",
            "overrides": overrides[:200],
            "total": len(overrides),
        }

    def get_overrides(object_name: str = "", method_name: str = "") -> dict:
        """Перехваченные методы из индекса (мгновенно).
        object_name/method_name — фильтры ('' = все).
        Возвращает: {overrides: [...], total: N, source: "index"|"live"|"unavailable"}"""
        # Try index first
        if idx_reader is not None:
            result = idx_reader.get_extension_overrides(object_name, method_name)
            if result is not None:
                return {
                    "overrides": result[:200],
                    "total": len(result),
                    "source": "index",
                }
        # Live fallback
        from rlm_tools_bsl.extension_detector import (
            detect_extension_context as _det,
            find_extension_overrides as _feo,
        )

        try:
            ctx = _det(base_path)
        except Exception:
            return {"overrides": [], "total": 0, "source": "unavailable"}

        from rlm_tools_bsl.extension_detector import ConfigRole

        all_overrides: list[dict] = []
        if ctx.current.role == ConfigRole.EXTENSION:
            all_overrides = _feo(base_path, object_name or None)
        elif ctx.current.role == ConfigRole.MAIN and ctx.nearby_extensions:
            for ext in ctx.nearby_extensions:
                try:
                    ovs = _feo(ext.path, object_name or None)
                    for ov in ovs:
                        ov["extension_name"] = ext.name
                        ov["extension_root"] = ext.path
                    all_overrides.extend(ovs)
                except Exception:
                    pass

        if method_name:
            all_overrides = [ov for ov in all_overrides if ov.get("target_method", "").lower() == method_name.lower()]

        return {
            "overrides": all_overrides[:200],
            "total": len(all_overrides),
            "source": "live",
        }

    # ── v1.9.0: find_references_to_object + find_defined_types ───────
    # Russian → English metadata prefix map (canonical singular form)
    _RU_META_PREFIXES: dict[str, str] = {
        "Справочник.": "Catalog.",
        "Документ.": "Document.",
        "Перечисление.": "Enum.",
        "РегистрСведений.": "InformationRegister.",
        "РегистрНакопления.": "AccumulationRegister.",
        "РегистрБухгалтерии.": "AccountingRegister.",
        "РегистрРасчета.": "CalculationRegister.",
        "ПланВидовХарактеристик.": "ChartOfCharacteristicTypes.",
        "ПланСчетов.": "ChartOfAccounts.",
        "ПланВидовРасчета.": "ChartOfCalculationTypes.",
        "ПланОбмена.": "ExchangePlan.",
        "ОпределяемыйТип.": "DefinedType.",
        "БизнесПроцесс.": "BusinessProcess.",
        "Задача.": "Task.",
        "Отчет.": "Report.",
        "Обработка.": "DataProcessor.",
        "Константа.": "Constant.",
        "Подсистема.": "Subsystem.",
        "Роль.": "Role.",
        "ОбщаяКоманда.": "CommonCommand.",
        "ФункциональнаяОпция.": "FunctionalOption.",
        "ПодпискаНаСобытие.": "EventSubscription.",
    }

    def _normalize_object_ref(s: str) -> tuple[str, list[str]]:
        """Normalize input object reference to canonical form (e.g. 'Catalog.X').

        Accepts Russian/English prefixes and Ref/Object/Manager/etc. forms.
        Returns (canonical, [canonical]) — match_forms list kept short because
        the index stores ref_object only in canonical form.
        """
        from rlm_tools_bsl.bsl_xml_parsers import canonicalize_type_ref as _ctr

        if not s:
            return ("", [])
        text = s.strip()
        # Convert Russian prefix to English (most common: "Справочник.X").
        # Case-insensitive on the prefix (casefold, Cyrillic-aware) so that
        # "ДОКУМЕНТ.X" / "документ.X" normalize the same as "Документ.X" — the
        # object NAME part is preserved as-is (its case is handled downstream by
        # object_ref_key/py_lower lookups).
        for ru, en in _RU_META_PREFIXES.items():
            if text[: len(ru)].casefold() == ru.casefold():
                text = en + text[len(ru) :]
                break
        # Already canonical form like "Catalog.X" passes through canonicalize unchanged.
        canonical = _ctr(text)
        if not canonical:
            # Could be just a name without prefix — assume Catalog as default? No, keep as-is.
            canonical = text
        return canonical, [canonical]

    # Priority for sorting + truncation
    _REF_KIND_PRIORITY: dict[str, int] = {
        "attribute_type": 0,
        "subsystem_content": 1,
        "exchange_plan_content": 2,
        "functional_option_content": 3,
        "event_subscription_source": 4,
        "role_rights": 5,
        "defined_type_content": 6,
        "characteristic_type": 7,
        "owner": 8,
        "based_on": 9,
        "choice_parameter_link": 10,
        "link_by_type": 11,
        "main_form": 12,
        "list_form": 13,
        "default_object_form": 14,
        "default_list_form": 15,
        "command_parameter_type": 16,
        "predefined_characteristic_type": 17,
    }

    def find_references_to_object(
        object_ref: str,
        kinds: list[str] | None = None,
        limit: int = 1000,
        include_code: bool = False,
    ) -> dict:
        """Find all references to a metadata object (Configurator "Найти ссылки → В свойствах" analogue).

        Covers declarative metadata-XML references (attribute types, owner, subsystems,
        functional options, rights, …). Pass include_code=True to additionally run
        find_code_usages and surface in-code usages under separate `code_*` keys.

        Args:
            object_ref: e.g. 'Справочник.Контрагенты' or 'Catalog.Контрагенты'.
            kinds: optional filter by ref_kind (see _REF_KIND_PRIORITY for the list).
            limit: maximum references returned (default 1000).
            include_code: also include in-code usages (find_code_usages) under
                top-level keys code_usages/code_total/code_by_kind/code_truncated/
                code_partial/code_meta. Metadata keys are unchanged.

        Returns:
            {object, references, total, truncated, partial, by_kind}
            (+ code_* keys when include_code=True).
        """

        def _finish(res: dict) -> dict:
            if include_code:
                code = find_code_usages(object_ref, limit=limit)
                res["code_usages"] = code["usages"]
                res["code_total"] = code["total"]
                res["code_by_kind"] = code["by_kind"]
                res["code_truncated"] = code["truncated"]
                res["code_partial"] = code["partial"]
                res["code_meta"] = code["_meta"]
            return res

        canonical, _ = _normalize_object_ref(object_ref)
        result: dict = {
            "object": canonical,
            "references": [],
            "total": 0,
            "truncated": False,
            "partial": False,
            "by_kind": {},
        }
        if not canonical or "." not in canonical:
            return _finish(result)

        if idx_reader is not None:
            # Authoritative total + by_kind FIRST (cheap GROUP BY count)
            try:
                counts = idx_reader.count_metadata_references(canonical, kinds=kinds)
            except Exception:
                counts = None
            try:
                # SQL already orders by ref_kind priority + path + used_in,
                # so passing exact `limit` keeps the highest-priority refs.
                rows = idx_reader.find_metadata_references(canonical, kinds=kinds, limit=limit)
            except Exception:
                rows = None
            if rows is not None:
                if counts is not None:
                    result["total"] = counts["total"]
                    result["by_kind"] = counts["by_kind"]
                    if counts["total"] > limit:
                        result["truncated"] = True
                else:
                    result["total"] = len(rows)
                    result["by_kind"] = _count_by_kind([{"kind": r["ref_kind"]} for r in rows])
                result["references"] = [
                    {
                        "used_in": r["used_in"],
                        "path": r["path"],
                        "line": r["line"],
                        "kind": r["ref_kind"],
                    }
                    for r in rows
                ]
                return _finish(result)

        # Fallback: live scan
        result["partial"] = True
        all_refs = list(_live_find_references(canonical, kinds))
        result["total"] = len(all_refs)
        result["by_kind"] = _count_by_kind(all_refs)
        all_refs.sort(key=lambda x: (_REF_KIND_PRIORITY.get(x["kind"], 99), x["path"], x["used_in"]))
        if len(all_refs) > limit:
            result["truncated"] = True
            all_refs = all_refs[:limit]
        result["references"] = all_refs
        return _finish(result)

    def find_code_usages(
        object_ref: str,
        kind: str | None = None,
        limit: int = 1000,
    ) -> dict:
        """Find where a metadata object is used IN CODE (reverse code-usage search).

        Complements find_references_to_object (which covers declarative metadata-XML
        references). Backed by the metadata_code_usages index table (builder v13+).

        Captures (light regex layer, source-aware):
          - 'manager'  — collection access `Документы.X` / `Documents.X`;
          - 'ref_type' — type in a string literal `"ДокументСсылка.X"` / `"DocumentRef.X"`;
          - 'query'    — metadata path in a query literal `Документ.X` and
                         `Документ.X.Товары` ('member' = tabular section name).
        Does NOT capture attribute access via local variables (`Док.Товары.Количество`).

        Scope: main configuration modules only (extensions are not in the index).

        Args:
            object_ref: 'Документ.X' / 'Document.X'. The metadata-type prefix is
                accepted in either RU or EN form, case-insensitively; the object
                NAME part is also matched case-insensitively (incl. Cyrillic) via
                the stored object_ref_key.
            kind: optional filter — 'manager' | 'ref_type' | 'query'.
            limit: maximum usages returned (default 1000).

        Returns:
            {object, usages: [{path, object_name, category, module_type, line, kind, member}],
             by_kind, total, truncated, partial, _meta: {scope, extensions_included}}.
            partial=True only when the index lacks the table (rebuild required).
        """
        canonical, _ = _normalize_object_ref(object_ref)
        result: dict = {
            "object": canonical,
            "usages": [],
            "by_kind": {},
            "total": 0,
            "truncated": False,
            "partial": False,
            "_meta": {"scope": "main_config", "extensions_included": False},
        }
        if not canonical or "." not in canonical:
            return result

        if idx_reader is not None:
            try:
                counts = idx_reader.count_code_usages(canonical, kind=kind)
            except Exception:
                counts = None
            try:
                rows = idx_reader.find_code_usages(canonical, kind=kind, limit=limit)
            except Exception:
                rows = None
            if rows is not None:
                # Table present — authoritative answer (empty is a valid answer).
                if counts is not None:
                    result["total"] = counts["total"]
                    result["by_kind"] = counts["by_kind"]
                    if counts["total"] > limit:
                        result["truncated"] = True
                else:
                    result["total"] = len(rows)
                    result["by_kind"] = _count_by_kind([{"kind": r["kind"]} for r in rows])
                result["usages"] = rows
                return result

        # Fallback: table missing (pre-v13 index) — limited live grep by short name.
        result["partial"] = True
        result["_meta"]["hint"] = (
            "metadata_code_usages table missing — rebuild the index (rlm_index) for fast, complete code-usage search"
        )
        short_name = canonical.split(".", 1)[1] if "." in canonical else canonical
        usages: list[dict] = []
        try:
            for hit in safe_grep(re.escape(short_name), max_files=40):
                usages.append(
                    {
                        "path": hit["file"],
                        "object_name": short_name,
                        "category": "",
                        "module_type": "",
                        "line": hit["line"],
                        "kind": "unknown",
                        "member": None,
                    }
                )
        except Exception:
            pass
        result["total"] = len(usages)
        result["by_kind"] = _count_by_kind(usages)
        if len(usages) > limit:
            result["truncated"] = True
            usages = usages[:limit]
        result["usages"] = usages
        return result

    def _count_by_kind(refs: list[dict]) -> dict:
        out: dict[str, int] = {}
        for r in refs:
            k = r.get("kind", "")
            out[k] = out.get(k, 0) + 1
        return out

    def _live_find_references(canonical: str, kinds: list[str] | None) -> list[dict]:
        """Live scan fallback when metadata_references table is not available.

        Walks Documents/Catalogs/Subsystems/etc., parses metadata XML on the fly.
        """
        from rlm_tools_bsl.bsl_xml_parsers import (
            canonicalize_type_ref as _ctr,
            parse_command_parameter_type as _pcpt,
            parse_defined_type as _pdt,
            parse_exchange_plan_content as _pep,
            parse_metadata_xml as _pmx,
            parse_pvh_characteristics as _ppc,
        )

        canonical_lower = canonical.lower()
        kinds_set = set(kinds) if kinds else None
        results: list[dict] = []

        _CATEGORY_TYPE: dict[str, str] = {
            "Documents": "Document",
            "Catalogs": "Catalog",
            "Enums": "Enum",
            "InformationRegisters": "InformationRegister",
            "AccumulationRegisters": "AccumulationRegister",
            "AccountingRegisters": "AccountingRegister",
            "CalculationRegisters": "CalculationRegister",
            "ChartsOfAccounts": "ChartOfAccounts",
            "ChartsOfCharacteristicTypes": "ChartOfCharacteristicTypes",
            "ChartsOfCalculationTypes": "ChartOfCalculationTypes",
            "ExchangePlans": "ExchangePlan",
            "BusinessProcesses": "BusinessProcess",
            "Tasks": "Task",
            "Subsystems": "Subsystem",
            "FunctionalOptions": "FunctionalOption",
            "EventSubscriptions": "EventSubscription",
            "Reports": "Report",
            "DataProcessors": "DataProcessor",
            "Constants": "Constant",
            "DocumentJournals": "DocumentJournal",
        }

        scan_categories = list(_CATEGORY_TYPE.keys())
        # CommonCommands is also a top-level category contributing refs
        if "CommonCommands" not in scan_categories:
            scan_categories.append("CommonCommands")
            _CATEGORY_TYPE["CommonCommands"] = "CommonCommand"

        seen_files: set[Path] = set()
        # Object-level dedup: when same logical object is parsed via sibling .xml AND
        # via Ext/<Type>.xml, the second pass would emit duplicate refs.
        # Key: (used_in, kind) — the same logical reference is unambiguous regardless
        # of source file path (in production both files have identical content).
        emitted_keys: set[tuple[str, str]] = set()

        import re as _re

        def _resolve_attr_line(suffix: str, lines: list[str]) -> int | None:
            """Same heuristic as bsl_index._line_for_ref — find <Name>X</Name> line."""
            if not suffix:
                return None
            target_name: str | None = None
            if suffix.startswith(("Attribute.", "Dimension.", "Resource.")):
                parts = suffix.split(".")
                if len(parts) >= 2:
                    target_name = parts[1]
            elif suffix.startswith("TabularSection.") and ".Attribute." in suffix:
                after = suffix.split(".Attribute.", 1)[1]
                target_name = after.split(".", 1)[0]
            if not target_name:
                return None
            pat = _re.compile(rf"<\s*[Nn]ame\s*>{_re.escape(target_name)}<\s*/\s*[Nn]ame\s*>")
            for idx, line in enumerate(lines, start=1):
                if pat.search(line):
                    return idx
            return None

        def _emit_from_xml(xml_path: Path, category: str, fallback_name: str) -> None:
            if xml_path in seen_files:
                return
            seen_files.add(xml_path)
            try:
                content = xml_path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                return
            try:
                parsed = _pmx(content)
            except Exception:
                return
            if not parsed:
                return
            obj_name = parsed.get("name") or fallback_name
            rel = xml_path.relative_to(Path(base_path)).as_posix()
            type_prefix = _CATEGORY_TYPE.get(category, category)
            used_in_root = f"{type_prefix}.{obj_name}"
            content_lines: list[str] | None = None
            for ref in parsed.get("references", []):
                if ref.get("ref_object", "").lower() != canonical_lower:
                    continue
                kind = ref.get("ref_kind", "")
                if kinds_set is not None and kind not in kinds_set:
                    continue
                suffix = ref.get("used_in_suffix", "")
                used_in = f"{used_in_root}.{suffix}" if suffix else used_in_root
                key = (used_in, kind)
                if key in emitted_keys:
                    continue
                emitted_keys.add(key)
                if content_lines is None:
                    content_lines = content.splitlines()
                line = _resolve_attr_line(suffix, content_lines)
                results.append({"used_in": used_in, "path": rel, "line": line, "kind": kind})

        def _emit_command_param_refs(
            xml_path: Path,
            host_category: str,
            host_object: str,
        ) -> None:
            """Emit command_parameter_type refs from a single Command XML/.command/.mdo.

            host_category is the top-level category for source_category accounting:
            'CommonCommands' for top-level commands, or 'Catalogs'/'Documents'/...
            for object-nested commands.
            host_object is the source_object label used in `used_in`:
            command name itself for CommonCommands, parent object name otherwise.
            """
            if kinds_set is not None and "command_parameter_type" not in kinds_set:
                return
            if xml_path in seen_files:
                return
            seen_files.add(xml_path)
            try:
                content = xml_path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                return
            try:
                cmd_refs = _pcpt(content)
            except Exception:
                return
            if not cmd_refs:
                return
            rel = xml_path.relative_to(Path(base_path)).as_posix()
            for ref in cmd_refs:
                ref_object = ref.get("ref_object", "")
                if ref_object.lower() != canonical_lower:
                    continue
                cmd_name = ref.get("command_name", "") or xml_path.stem
                if host_category == "CommonCommands":
                    used_in = f"CommonCommand.{cmd_name}.CommandParameterType"
                else:
                    type_prefix = _CATEGORY_TYPE.get(host_category, host_category)
                    used_in = f"{type_prefix}.{host_object}.Command.{cmd_name}.CommandParameterType"
                key = (used_in, "command_parameter_type")
                if key in emitted_keys:
                    continue
                emitted_keys.add(key)
                results.append(
                    {
                        "used_in": used_in,
                        "path": rel,
                        "line": None,
                        "kind": "command_parameter_type",
                    }
                )

        # Walk every category: cover BOTH layouts
        # 1) <Category>/<Object>/{Object.mdo|Ext/<Type>.xml} (Catalogs/Documents/...)
        # 2) <Category>/<Object>.xml (top-level — Subsystems/X.xml, FunctionalOptions/X.xml,
        #    EventSubscriptions/X.xml, CommonCommands/X.xml — plus Subsystem nesting)
        for category in scan_categories:
            cat_dir = Path(base_path) / category
            if not cat_dir.is_dir():
                continue

            # Track layout-1 stems to avoid re-parsing the same logical object via
            # the sibling layout-2 pass (Catalogs/X/ + Catalogs/X.xml — same content).
            covered_stems: set[str] = set()

            # Layout 1: object subdirectories
            for obj_dir in cat_dir.iterdir():
                if not obj_dir.is_dir():
                    continue
                obj_name = obj_dir.name
                xml_path = None
                mdo = obj_dir / f"{obj_name}.mdo"
                if mdo.is_file():
                    xml_path = mdo
                else:
                    sibling = obj_dir.parent / f"{obj_name}.xml"
                    if sibling.is_file():
                        xml_path = sibling
                    else:
                        ext_dir = obj_dir / "Ext"
                        if ext_dir.is_dir():
                            for fp in sorted(ext_dir.iterdir()):
                                if fp.suffix.lower() == ".xml" and fp.is_file():
                                    xml_path = fp
                                    break
                if xml_path is not None:
                    _emit_from_xml(xml_path, category, obj_name)
                    covered_stems.add(obj_name)

                # Object-nested commands: <Cat>/<Obj>/Commands/<Cmd>.xml or
                # <Cat>/<Obj>/Commands/<Cmd>/<Cmd>.command (EDT)
                if category != "CommonCommands":
                    cmd_dir = obj_dir / "Commands"
                    if cmd_dir.is_dir():
                        for cmd_entry in cmd_dir.iterdir():
                            if cmd_entry.is_file() and cmd_entry.suffix.lower() == ".xml":
                                _emit_command_param_refs(cmd_entry, category, obj_name)
                            elif cmd_entry.is_dir():
                                for cand in (
                                    cmd_entry / f"{cmd_entry.name}.command",
                                    cmd_entry / f"{cmd_entry.name}.mdo",
                                ):
                                    if cand.is_file():
                                        _emit_command_param_refs(cand, category, obj_name)
                                        break

            # Layout 2: top-level *.xml / *.mdo files; skip files whose stem already
            # covered by a layout-1 obj-dir to avoid duplicate refs.
            for fp in cat_dir.rglob("*"):
                if not fp.is_file():
                    continue
                if fp.suffix.lower() not in (".xml", ".mdo"):
                    continue
                # Skip top-level sibling already handled by layout 1.
                if fp.parent == cat_dir and fp.stem in covered_stems:
                    continue
                # CommonCommands deserves command-parameter-type extraction in addition to
                # the regular metadata parse pass.
                if category == "CommonCommands":
                    _emit_command_param_refs(fp, "CommonCommands", fp.stem)
                _emit_from_xml(fp, category, fp.stem)

        # ExchangePlans content
        ep_dir = Path(base_path) / "ExchangePlans"
        if ep_dir.is_dir() and (kinds_set is None or "exchange_plan_content" in kinds_set):
            for plan_dir in ep_dir.iterdir():
                if not plan_dir.is_dir():
                    continue
                plan_name = plan_dir.name
                files = [plan_dir / "Ext" / "Content.xml", plan_dir / f"{plan_name}.mdo"]
                for fp in files:
                    if not fp.is_file():
                        continue
                    try:
                        text = fp.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    items = _pep(text)
                    if not items:
                        continue
                    rel = fp.relative_to(Path(base_path)).as_posix()
                    for item in items:
                        canon = _ctr(item.get("ref", ""))
                        if canon.lower() == canonical_lower:
                            results.append(
                                {
                                    "used_in": f"ExchangePlan.{plan_name}.Content",
                                    "path": rel,
                                    "line": None,
                                    "kind": "exchange_plan_content",
                                }
                            )

        # DefinedTypes
        dt_dir = Path(base_path) / "DefinedTypes"
        if dt_dir.is_dir() and (kinds_set is None or "defined_type_content" in kinds_set):
            for fp in dt_dir.iterdir():
                paths_to_try: list[Path] = []
                if fp.is_file() and fp.suffix.lower() == ".xml":
                    paths_to_try.append(fp)
                elif fp.is_dir():
                    mdo = fp / f"{fp.name}.mdo"
                    if mdo.is_file():
                        paths_to_try.append(mdo)
                for cfp in paths_to_try:
                    try:
                        text = cfp.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    parsed_dt = _pdt(text)
                    if not parsed_dt:
                        continue
                    rel = cfp.relative_to(Path(base_path)).as_posix()
                    for type_str in parsed_dt.get("types", []):
                        canon = _ctr(type_str)
                        if canon.lower() == canonical_lower:
                            results.append(
                                {
                                    "used_in": f"DefinedType.{parsed_dt['name']}.Type",
                                    "path": rel,
                                    "line": None,
                                    "kind": "defined_type_content",
                                }
                            )

        # ChartsOfCharacteristicTypes characteristic_types (Type list at top level)
        # Already covered via parse_metadata_xml path above (characteristic_type kind)
        # but parse_pvh_characteristics provides a clean list — reuse just for completeness.
        _ = _ppc  # parse_pvh_characteristics covered indirectly via parse_metadata_xml
        return results

    def find_defined_types(name: str) -> dict:
        """Resolve a DefinedType by name to its concrete type list.

        Args:
            name: e.g. 'Сумма' or 'ОпределяемыйТип.Сумма' or 'DefinedType.Сумма'.

        Returns:
            {name, types: list[str], path: str, partial: bool}.
            On v11 indexes (no defined_types table) does live XML scan.
        """
        text = name.strip()
        # strip prefix
        for prefix in ("ОпределяемыйТип.", "DefinedType."):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        result: dict = {"name": text, "types": [], "path": "", "partial": False}

        if idx_reader is not None:
            try:
                row = idx_reader.find_defined_type(text)
            except Exception:
                row = None
            if row is not None:
                return {"name": row["name"], "types": row["types"], "path": row["path"], "partial": False}

        # Live fallback
        from rlm_tools_bsl.bsl_xml_parsers import (
            canonicalize_type_ref as _ctr,
            parse_defined_type as _pdt,
        )

        result["partial"] = True
        dt_dir = Path(base_path) / "DefinedTypes"
        if not dt_dir.is_dir():
            return result
        text_lower = text.lower()
        for fp in dt_dir.iterdir():
            paths: list[Path] = []
            if fp.is_file() and fp.suffix.lower() == ".xml":
                paths.append(fp)
            elif fp.is_dir():
                mdo = fp / f"{fp.name}.mdo"
                if mdo.is_file():
                    paths.append(mdo)
            for cfp in paths:
                try:
                    content = cfp.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                parsed = _pdt(content)
                if not parsed or parsed["name"].lower() != text_lower:
                    continue
                from rlm_tools_bsl.bsl_xml_parsers import _XS_TYPE_MAP, _strip_ns_prefix

                canonical_types: list[str] = []
                for type_str in parsed.get("types", []):
                    canon = _ctr(type_str)
                    if canon:
                        canonical_types.append(canon)
                        continue
                    stripped = type_str.strip()
                    mapped = _XS_TYPE_MAP.get(stripped) or _XS_TYPE_MAP.get(f"xs:{stripped}")
                    canonical_types.append(mapped or _strip_ns_prefix(stripped))
                rel = cfp.relative_to(Path(base_path)).as_posix()
                result.update({"name": parsed["name"], "types": canonical_types, "path": rel})
                return result
        return result

    # ── Register all helpers ─────────────────────────────────────
    # Each _reg() call: name, function, signature (for strategy table),
    # category (for grouping), keywords (for help search), recipe (code example).
    # Adding a new helper = define function above + add _reg() here.

    _reg("find_module", find_module, "find_module(name) -> [{path, category, object_name, module_type}]", "discovery")
    _reg(
        "find_by_type",
        find_by_type,
        "find_by_type(category, name='') -> same. Categories: Documents, Catalogs, CommonModules, InformationRegisters, AccumulationRegisters, Reports, DataProcessors",
        "discovery",
    )

    _reg(
        "extract_procedures",
        extract_procedures,
        "extract_procedures(path) -> [{name, type, line, end_line, is_export, params}]",
        "code",
    )
    _reg(
        "find_exports",
        find_exports,
        "find_exports(path) -> [{name, line, is_export, type, params}]",
        "code",
        ["export", "экспорт", "find_exports", "процедур", "функци"],
        "FIND EXPORTS:\n"
        "  modules = find_module('Name')  # replace 'Name'\n"
        "  if not modules:\n"
        "      print('Не найдено')\n"
        "  else:\n"
        "      path = modules[0]['path']\n"
        "      exports = find_exports(path)\n"
        "      for e in exports:\n"
        "          print(e['name'], 'line:', e['line'], 'export:', e['is_export'])",
    )
    _reg(
        "read_procedure",
        read_procedure,
        "read_procedure(path, proc_name, include_overrides=False) -> str | None (numbered in MCP session)",
        "code",
        ["read", "чтени", "читать", "содержим", "content", "тело", "body"],
        "READ PROCEDURE BODY:\n"
        "  modules = find_module('Name')\n"
        "  if not modules:\n"
        "      print('Не найдено')\n"
        "  else:\n"
        "      path = modules[0]['path']\n"
        "      body = read_procedure(path, 'ProcedureName')  # numbered in MCP session\n"
        "      if body is None:\n"
        "          # имя неточное или у объекта только XML-метаданные (КОДСобытия и т.п.)\n"
        "          procs = extract_procedures(path)\n"
        "          for p in procs:\n"
        "              print(p['name'], 'export=', p['is_export'])\n"
        "      else:\n"
        "          print(body)\n"
        "  # Если расширения перехватили метод — читать с перехватами:\n"
        "  full = read_procedure(path, 'ProcName', include_overrides=True)\n"
        "  # full = оригинал + '=== Перехвачен &Аннотация в расширении X ===' + тело перехвата",
    )
    _reg(
        "find_callers_context",
        find_callers_context,
        "find_callers_context(proc, module_hint, 0, 50) -> {callers: [{file, caller_name, line, ...}], _meta: {total_callers, returned, offset, has_more}}",
        "code",
        ["caller", "call graph", "граф", "вызов", "вызыва", "кто вызывает", "find_callers"],
        "BUILD CALL GRAPH:\n"
        "  # With index: instant across the whole codebase, hint is optional\n"
        "  # Without index: parallel file scan, hint narrows scope\n"
        "  modules = find_module('Name')\n"
        "  if not modules:\n"
        "      print('Не найдено')\n"
        "  else:\n"
        "      path = modules[0]['path']\n"
        "      exports = find_exports(path)\n"
        "      for e in exports:\n"
        "          data = find_callers_context(e['name'], '', 0, 50)\n"
        "          for c in data['callers']:\n"
        "              print(e['name'], '<-', c['caller_name'], c['file'], 'line:', c['line'])\n"
        "          if data['_meta']['has_more']:\n"
        "              print('  ... more callers, increase offset')",
    )
    _reg(
        "find_call_hierarchy",
        find_call_hierarchy,
        "find_call_hierarchy(name, direction='callers', depth=2) -> "
        "{root, direction, depth, tree:[{name, callers:[{caller_name, module_path, category, "
        "object_name, line, is_export, level}]}], visited:int, "
        "truncated_targets:[{name, level, total, returned}]} | {error, hint, supported_directions}",
        "code",
        [
            "иерархия вызовов",
            "call hierarchy",
            "граф вызовов",
            "цепочка вызовов",
            "depth",
            "глубина",
            "транзитивный",
        ],
        "BUILD CALL HIERARCHY (multi-level callers tree):\n"
        "  # depth=1..3 (по умолчанию 2). Только direction='callers' в v1.10.0.\n"
        "  res = find_call_hierarchy('ОбработкаПроведения', direction='callers', depth=2)\n"
        "  if 'error' in res:\n"
        "      print(res['hint'])  # callees/both пока не поддержаны\n"
        "  else:\n"
        "      for node in res['tree']:\n"
        "          for c in node['callers']:\n"
        "              print(f\"  L{c['level']} {c['caller_name']} <- {c['object_name']} ({c['module_path']}:{c['line']})\")\n"
        "      # Если на каком-то таргете callers > 200 — узел truncated, дерево неполное:\n"
        "      for t in res['truncated_targets']:\n"
        "          print(f\"  TRUNCATED: {t['name']} (L{t['level']}): returned {t['returned']}/{t['total']}\")\n"
        "          # для полного списка callers конкретного метода — find_callers_context(t['name'], '', offset=200, limit=200)\n"
        "  # Одноимённые методы возвращают список носителей — выбирай по object_name/category\n"
        "  # Для глубины 1 эффективнее обычный find_callers_context()",
    )
    _reg(
        "find_callers",
        find_callers,
        "find_callers(proc, hint, max_files=20) -> [{file, line, text}]  # COMPACT FIRST PAGE: thin wrapper над find_callers_context, default limit=20, без _meta/has_more — quick view; для полного аудита callers — find_callers_context",
        "code",
        ["compact callers", "плоский список вызовов", "только пути вызовов"],
        "COMPACT FIRST PAGE OF CALLERS (для quick view: 3 поля вместо 7, без пагинации):\n"
        "  hits = find_callers(proc, hint, max_files=20)\n"
        "  for h in hits:\n"
        "      print(h['file'], 'line:', h['line'], h['text'])\n"
        "  # Когда брать find_callers vs find_callers_context:\n"
        "  #   find_callers          → quick view, первые max_files (default 20). Без has_more —\n"
        "  #                           если callers > max_files, остаток молча отбрасывается.\n"
        "  #   find_callers_context  → полный API: caller_name, object_name, category, is_export\n"
        "  #                           + _meta с total_callers/has_more и пагинация (offset/limit).\n"
        "  # Под капотом find_callers вызывает find_callers_context — поиск тот же, но контракт\n"
        "  # урезан. Для аудита/полного списка — всегда find_callers_context.",
    )
    _reg(
        "safe_grep",
        safe_grep,
        "safe_grep(pattern, hint, max_files=20) -> [{file, line, text}]",
        "code",
        ["search", "grep", "поиск", "искать", "найти", "pattern", "шаблон"],
        "SEARCH FOR CODE:\n"
        "  results = safe_grep('SearchPattern', 'ModuleHint', max_files=20)\n"
        "  for r in results:\n"
        "      print(r['file'], 'line:', r['line'], r['text'])\n"
        "  # Or find modules by name:\n"
        "  modules = find_module('PartOfName')\n"
        "  if not modules:\n"
        "      print('Не найдено')\n"
        "  else:\n"
        "      for m in modules:\n"
        "          print(m['path'], m['category'], m['object_name'])",
    )

    _reg(
        "parse_object_xml",
        parse_object_xml,
        "parse_object_xml(path) -> {name, synonym, attributes, tabular_sections, dimensions, resources, ...}",
        "xml",
        [
            "metadata",
            "метаданн",
            "реквизит",
            "attribute",
            "dimension",
            "измерен",
            "ресурс",
            "resource",
            "табличн",
            "tabular",
            "xml",
            "parse_object",
        ],
        "READ METADATA:\n"
        "  # Accepts directory or XML path — auto-resolves:\n"
        "  meta = parse_object_xml('Documents/РеализацияТоваровУслуг')  # directory\n"
        "  meta = parse_object_xml('Documents/Name/Ext/Document.xml')   # direct XML\n"
        "  # Также принимает 'фейковый' .mdo-путь — авто-нормализует base:\n"
        "  meta = parse_object_xml('Documents/X.mdo')   # => Documents/X/X.mdo (EDT) или Ext/Document.xml (CF)\n"
        "  # Если ничего не найдено — FileNotFoundError с явной подсказкой про директорию.\n"
        "  for key in meta:\n"
        "      print(key, ':', meta[key])",
    )
    _reg(
        "parse_form",
        parse_form,
        "parse_form(object_name, form_name='', handler='') -> [{form_name, module_path, handlers, commands, attributes}]",
        "xml",
        kw=["parse_form", "события формы", "обработчики формы", "элементы формы", "form handler", "form event"],
        recipe=(
            "# Обработчики и команды формы объекта:\n"
            "forms = parse_form('БанковскиеСчетаОрганизаций')\n"
            "for f in forms:\n"
            '    print(f\'{f["form_name"]}: {len(f["handlers"])} handlers, {len(f["commands"])} commands\')\n'
            "    for h in f['handlers']:\n"
            '        print(f\'  {h["element"] or "[form]"}.{h["event"]} → {h["handler"]}\')\n\n'
            "# Обратный поиск: к чему привязана процедура?\n"
            "forms = parse_form('БанковскиеСчетаОрганизаций', handler='ПриСозданииНаСервере')\n\n"
            "# module_path для быстрого перехода к коду:\n"
            "for f in forms:\n"
            "    if f['module_path']:\n"
            "        procs = extract_procedures(f['module_path'])\n"
            "        print(f'{f[\"form_name\"]}: {len(procs)} procedures')\n"
        ),
    )
    _reg(
        "find_enum_values",
        find_enum_values,
        "find_enum_values(enum_name) -> {name, synonym, values: [{name, synonym}]}",
        "xml",
        ["перечислен", "enum", "значени перечислени"],
        "FIND ENUM VALUES:\n"
        "  result = find_enum_values('СтатусыЗаказовКлиентов')\n"
        "  print(f\"{result['name']} ({result['synonym']})\")\n"
        "  for v in result['values']:\n"
        "      print(f\"  {v['name']}: {v['synonym']}\")",
    )
    _reg(
        "find_attributes",
        find_attributes,
        "find_attributes(name='', object_name='', category='', kind='', limit=500) -> [{object_name, category, attr_name, attr_synonym, attr_type, attr_kind, ts_name}]",
        "xml",
        [
            "реквизит",
            "attribute",
            "тип",
            "type",
            "измерение",
            "dimension",
            "ресурс",
            "resource",
            "колонка",
            "табличная часть",
        ],
        "FIND ATTRIBUTE TYPES:\n"
        "  # By attribute name:\n"
        "  results = find_attributes('Организация')\n"
        "  for r in results:\n"
        "      print(r['object_name'], r['attr_name'], r['attr_type'])\n"
        "  # All attributes of a document:\n"
        "  attrs = find_attributes(object_name='РеализацияТоваровУслуг')\n"
        "  # Only dimensions of a register:\n"
        "  dims = find_attributes(object_name='ТоварыОрганизаций', kind='dimension')\n"
        "  # БЕЗ ИНДЕКСА: find_attributes(name='X') без object_name вернёт [] — невозможно сканировать всю кодовую базу.\n"
        "  # Решение: всегда передавай object_name на проектах без индекса.",
    )
    _reg(
        "find_predefined",
        find_predefined,
        "find_predefined(name='', object_name='', limit=500) -> [{object_name, category, item_name, item_synonym, types, item_code}]",
        "xml",
        ["предопределённ", "predefined", "субконто", "subconto", "счёт", "account", "предопределенн"],
        "FIND PREDEFINED ITEMS:\n"
        "  # By name (subconto type question):\n"
        "  items = find_predefined('РеализуемыеАктивы')\n"
        "  for i in items:\n"
        "      print(i['item_name'], i['types'])\n"
        "  # All predefined of an object:\n"
        "  all_sub = find_predefined(object_name='ВидыСубконтоХозрасчетные')\n"
        "  # Predefined of a catalog:\n"
        "  countries = find_predefined(object_name='СтраныМира')\n"
        "  # БЕЗ ИНДЕКСА: find_predefined(name='X') без object_name вернёт [] — невозможно сканировать всю кодовую базу.\n"
        "  # Решение: всегда передавай object_name на проектах без индекса.",
    )

    _reg(
        "analyze_object",
        analyze_object,
        "analyze_object(name) -> full profile: metadata + modules + procedures + exports",
        "composite",
        ["profile", "профиль", "обзор", "overview", "analyze_object"],
        "OBJECT PROFILE:\n"
        "  result = analyze_object('АвансовыйОтчет')\n"
        "  meta = result.get('metadata', {})\n"
        "  print(f\"Объект: {result['name']} ({meta.get('synonym', '')})\")\n"
        "  print(f\"Реквизитов: {len(meta.get('attributes', []))}\")\n"
        "  for m in result.get('modules', []):\n"
        "      print(f\"  {m['module_type']}: {m['procedures_count']} проц, {m['exports_count']} эксп\")",
    )
    _reg(
        "get_object_full_structure",
        get_object_full_structure,
        "get_object_full_structure(name) -> {object_name, category, synonym, posting, attributes, "
        "tabular_sections:[{name, synonym, columns}], dimensions, resources, predefined_items, "
        "enum_values_for_typed_refs:{Enum.X:[{name,synonym}]}, forms:[str], "
        "_meta:{index_used: bool — True когда возвращённые структурные секции взяты из индекса "
        "(контракт об ИСТОЧНИКЕ, не о ПОЛНОТЕ — для проверки полноты на stale-индексе вызывай parse_object_xml); "
        "fallback_reason: 'index_unavailable_or_table_missing' | 'index_empty_for_object' | "
        "'category_without_attributes_filled_via_live_xml' | 'index_partially_enriched_from_live_xml' | "
        "'parse_failed: ...' | None; "
        "ts_synonyms_available: bool — True ТОЛЬКО если у хотя бы одной TS в результате непустой synonym}}",
        "composite",
        [
            "структура объекта",
            "полная структура",
            "карточка объекта",
            "object structure",
            "object profile",
            "вся структура",
            "реквизиты документа",
            "реквизиты справочника",
            "табличные части",
            "колонки тч",
        ],
        "FULL OBJECT STRUCTURE (1 вызов вместо 3-5 — заменяет parse_object_xml + find_attributes + find_predefined + find_enum_values):\n"
        "  # ⚠️ КЛЮЧИ В РЕЗУЛЬТАТЕ ОТЛИЧАЮТСЯ от find_attributes!\n"
        "  #   find_attributes:           [{attr_name, attr_synonym, attr_type, attr_kind}]\n"
        "  #   get_object_full_structure: {attributes:[{name, synonym, type}], dimensions:[...], resources:[...], ...}\n"
        "  #   Итерация: for a in s['attributes']: a['name']  (НЕ a['attr_name'] — будет KeyError)\n"
        "  s = get_object_full_structure('РеализацияТоваровУслуг')\n"
        "  print(f\"{s['object_name']} ({s.get('synonym')}) posting={s.get('posting')}\")\n"
        "  print(f\"Реквизитов: {len(s['attributes'])}, ТЧ: {len(s['tabular_sections'])}, форм: {len(s['forms'])}\")\n"
        "  for ts in s['tabular_sections']:\n"
        "      print(f\"  ТЧ {ts['name']}: {len(ts['columns'])} колонок\")\n"
        "  # Перечисления уже раскрыты:\n"
        "  for ref_type, values in s['enum_values_for_typed_refs'].items():\n"
        "      print(f\"  {ref_type}: {[v['name'] for v in values]}\")\n"
        "  # Для регистров — данные в dimensions/resources, attributes пустой:\n"
        "  reg = get_object_full_structure('ТоварыНаСкладах')  # AccumulationRegister\n"
        "  for d in reg.get('dimensions', []):\n"
        "      print(f\"  измерение {d['name']}: {d['type']}\")\n"
        "  for r in reg.get('resources', []):\n"
        "      print(f\"  ресурс {r['name']}: {r['type']}\")\n"
        "  # _meta.index_used=False означает live XML fallback (синонимы ТЧ доступны только в этом режиме)\n"
        "  if not s['_meta']['index_used']:\n"
        "      print('Fallback:', s['_meta']['fallback_reason'])",
    )
    _reg(
        "analyze_document_flow",
        analyze_document_flow,
        "analyze_document_flow(doc_name) -> metadata + subscriptions + register movements + jobs",
        "composite",
        ["lifecycle", "жизненн", "flow", "end-to-end", "полный анализ", "как работает"],
        "FULL DOCUMENT LIFECYCLE:\n"
        "  flow = analyze_document_flow('АвансовыйОтчет')\n"
        "  print('Подписки:', len(flow['event_subscriptions']))\n"
        "  for s in flow['event_subscriptions']:\n"
        "      print(f\"  {s['event']}: {s['handler']}\")\n"
        "  regs = flow['register_movements'].get('code_registers', [])\n"
        "  print('Регистры:', len(regs))\n"
        "  for r in regs:\n"
        "      print(f\"  Движения.{r['name']}\")",
    )
    _reg(
        "analyze_subsystem",
        analyze_subsystem,
        "analyze_subsystem(name) -> composition, custom vs standard objects",
        "composite",
        ["subsystem", "подсистем", "состав подсистем"],
        "ANALYZE SUBSYSTEM:\n"
        "  result = analyze_subsystem('Спецодежда')\n"
        "  for sub in result.get('subsystems', []):\n"
        "      print(f\"Подсистема: {sub['name']} ({sub['synonym']})\")\n"
        "      print(f\"Нетиповых: {len(sub['custom_objects'])}, типовых: {len(sub['standard_objects'])}\")\n"
        "      for obj in sub['custom_objects']:\n"
        "          print(f\"  [нетип] {obj['type']}.{obj['name']}\")\n"
        "      for obj in sub['standard_objects']:\n"
        "          print(f\"  [типов] {obj['type']}.{obj['name']}\")",
    )
    _reg(
        "find_custom_modifications",
        find_custom_modifications,
        "find_custom_modifications(obj, custom_prefixes=None) -> custom procedures, regions, attributes",
        "composite",
        ["custom", "нетипов", "доработк", "модификац", "modification"],
        "FIND CUSTOM MODIFICATIONS:\n"
        "  result = find_custom_modifications('ВнутреннееПотребление')\n"
        "  for mod in result.get('modifications', []):\n"
        "      print(f\"Модуль: {mod['path']}\")\n"
        "      for p in mod['custom_procedures']:\n"
        "          print(f\"  {p['type']} {p['name']} (стр.{p['line']})\")\n"
        "      for r in mod['custom_regions']:\n"
        "          print(f\"  #Область {r['name']} (стр.{r['line']})\")\n"
        "  for attr in result.get('custom_attributes', []):\n"
        "      print(f\"Реквизит: {attr['name']} ({attr.get('synonym', '')})\")",
    )

    _reg(
        "find_event_subscriptions",
        find_event_subscriptions,
        "find_event_subscriptions(obj, custom_only=False, event_filter=None, limit=None) -> list[dict] (default) | {subscriptions, total, returned, has_more} (when limit set)",
        "business",
        ["подписк", "subscription", "событи", "event", "BeforeWrite", "OnWrite", "ПриЗаписи", "ПередЗаписью"],
        "FIND EVENT SUBSCRIPTIONS:\n"
        "  # Default — весь список (контракт прежний):\n"
        "  subs = find_event_subscriptions('АвансовыйОтчет')\n"
        "  for s in subs: print(s['event'], s['handler'])\n"
        "  # С фильтром по событию (case-insensitive substring) — list[str] ИЛИ одна строка:\n"
        "  before_write = find_event_subscriptions('АвансовыйОтчет', event_filter=['BeforeWrite','ПередЗаписью'])\n"
        "  before_write_one = find_event_subscriptions('АвансовыйОтчет', event_filter='BeforeWrite')  # ок: одна строка\n"
        "  # С пагинацией (формат меняется на dict!):\n"
        "  page = find_event_subscriptions('', limit=50)\n"
        "  # page = {'subscriptions': [...], 'total': N, 'returned': K, 'has_more': bool}\n"
        "  if page['has_more']: ...  # увеличить limit или сузить event_filter",
    )
    _reg(
        "find_scheduled_jobs",
        find_scheduled_jobs,
        "find_scheduled_jobs(name='') -> [{name, method_name, use, ...}]",
        "business",
        ["регламент", "schedule", "job", "задани", "фонов", "background"],
        "FIND SCHEDULED JOBS:\n"
        "  # With index: instant. Without: parses XML on first call.\n"
        "  jobs = find_scheduled_jobs('Курс')\n"
        "  for j in jobs:\n"
        "      print(f\"{j['name']}: {j['method_name']} (active={j['use']})\")",
    )
    _reg(
        "find_register_movements",
        find_register_movements,
        "find_register_movements(doc_name) -> {code_registers, erp_mechanisms, manager_tables, adapted_registers}",
        "business",
        ["движени", "movement", "регистр", "register", "проведен", "posting"],
        "TRACE DOCUMENT REGISTER MOVEMENTS:\n"
        "  result = find_register_movements('ПриобретениеТоваровУслуг')\n"
        "  for r in result['code_registers']:\n"
        "      detail = r.get('lines') or r.get('source', '')\n"
        "      print(f\"  Движения.{r['name']} ({detail})\")\n"
        "  # Если документ непроводимый — результат содержит is_postable=False + hint:\n"
        "  if result.get('is_postable') is False:\n"
        "      print(result['hint'])  # подсказка про подписки/регистры сведений\n"
        "\n"
        "FIND WHO WRITES TO REGISTER:\n"
        "  result = find_register_writers('ТоварыНаСкладах')\n"
        "  for w in result['writers']:\n"
        "      detail = w.get('lines') or w.get('source', '')\n"
        "      print(f\"  {w['document']} ({detail})\")",
    )
    _reg(
        "find_register_writers",
        find_register_writers,
        "find_register_writers(reg_name) -> {writers: [{document, source|lines, file}]}",
        "business",
        ["писатели регистра", "кто пишет", "register writer", "writer"],
        "FIND WHO WRITES TO REGISTER (обратное к find_register_movements):\n"
        "  result = find_register_writers('ТоварыНаСкладах')\n"
        "  for w in result['writers']:\n"
        "      detail = w.get('lines') or w.get('source', '')\n"
        "      print(f\"  {w['document']} ({detail})\")\n"
        "  # Связка: find_register_movements(doc) ↔ find_register_writers(reg) — двусторонний поиск.",
    )
    _reg(
        "find_based_on_documents",
        find_based_on_documents,
        "find_based_on_documents(doc_name) -> {can_create_from_here, can_be_created_from}",
        "business",
        ["основани", "ввод на основании", "создать на основании", "based on", "filling", "заполнени"],
        "FIND BASED-ON DOCUMENTS (ввод на основании):\n"
        "  result = find_based_on_documents('ПриобретениеТоваровУслуг')\n"
        "  print('Можно создать из этого документа:')\n"
        "  for d in result['can_create_from_here']:\n"
        "      via = d.get('via', 'direct')  # 'direct' или 'back_scan' (обратный обход)\n"
        "      print(f\"  -> {d['document']} ({via})\")\n"
        "  print('Этот документ создается на основании:')\n"
        "  for d in result['can_be_created_from']:\n"
        "      print(f\"  <- {d['type']}\")\n"
        "  # Если у документа нет ДобавитьКомандыСозданияНаОсновании (типичный кейс — Письма в ДО3),\n"
        "  # хелпер автоматически делает back_scan по ОбработкаЗаполнения других Documents и находит\n"
        "  # документы, у которых наш doc_name упомянут как ДокументСсылка.<doc_name>.\n"
        "  # Записи из back_scan помечены via='back_scan'.",
    )
    _reg(
        "find_print_forms",
        find_print_forms,
        "find_print_forms(obj_name) -> {print_forms: [{name, presentation}]}",
        "business",
        ["печат", "print", "макет", "template", "накладн"],
        "FIND PRINT FORMS:\n"
        "  result = find_print_forms('РеализацияТоваровУслуг')\n"
        "  for p in result['print_forms']:\n"
        "      print(f\"  {p['name']}: {p['presentation']}\")",
    )
    _reg(
        "find_functional_options",
        find_functional_options,
        "find_functional_options(obj_name) -> {xml_options, code_options}",
        "business",
        ["функциональн", "опци", "functional", "option", "включен", "выключен"],
        "FIND FUNCTIONAL OPTIONS:\n"
        "  # With index: XML options instant. Code grep still runs live.\n"
        "  result = find_functional_options('РеализацияТоваровУслуг')\n"
        "  for fo in result['xml_options']:\n"
        "      print(f\"  {fo['name']}: {fo['synonym']}\")\n"
        "  for co in result['code_options']:\n"
        "      print(f\"  В коде: {co['option_name']} (стр.{co['line']})\")",
    )
    _reg(
        "find_roles",
        find_roles,
        "find_roles(obj_name) -> {roles: [{role_name, rights}]}",
        "business",
        ["роль", "role", "прав", "right", "доступ", "access", "разрешен"],
        "FIND ROLES AND RIGHTS:\n"
        "  result = find_roles('ПриобретениеТоваровУслуг')\n"
        "  for r in result['roles']:\n"
        "      print(f\"  {r['role_name']}: {', '.join(r['rights'])}\")",
    )

    _reg(
        "extract_queries",
        extract_queries,
        "extract_queries(path) -> [{procedure, line, tables, text_preview}]",
        "code",
        ["запрос", "query", "таблиц", "table", "select", "выбрать"],
        "EXTRACT QUERIES FROM MODULE:\n"
        "  queries = extract_queries('path/to/ObjectModule.bsl')\n"
        "  for q in queries:\n"
        "      print(f\"  {q['procedure']} стр.{q['line']}: таблицы={q['tables']}\")\n"
        "      print(f\"    {q['text_preview'][:100]}\")",
    )
    _reg(
        "code_metrics",
        code_metrics,
        "code_metrics(path) -> {total_lines, code_lines, comment_lines, procedures_count, avg_proc_size, max_nesting}",
        "code",
        ["метрик", "metric", "размер", "size", "complex", "сложност", "статистик", "statistic"],
        "CODE METRICS:\n"
        "  m = code_metrics('path/to/Module.bsl')\n"
        "  print(f\"Строк: {m['total_lines']} (код: {m['code_lines']}, комментарии: {m['comment_lines']})\")\n"
        "  print(f\"Процедур: {m['procedures_count']}, экспортных: {m['exports_count']}\")\n"
        "  print(f\"Средний размер: {m['avg_proc_size']} строк, макс. вложенность: {m['max_nesting']}\")",
    )

    _reg(
        "search_methods",
        search_methods,
        "search_methods(query, limit=30) -> [{name, type, is_export, module_path, object_name, rank}]",
        "discovery",
        ["поиск метод", "search", "fts", "full-text", "найти метод", "подстрок"],
        "SEARCH METHODS BY NAME (FTS5, requires pre-built index with --no-fts NOT set):\n"
        "  # Find methods by substring across the entire codebase — instant\n"
        "  results = search_methods('ОбработкаЗаполнения')\n"
        "  for r in results:\n"
        "      print(f\"  {r['name']} ({r['type']}) export={r['is_export']} in {r['module_path']}\")\n"
        "  # Returns [] if index or FTS not available\n"
        "  # Combine with read_procedure() to read found methods:\n"
        "  #   body = read_procedure(r['module_path'], r['name'])",
    )
    _reg(
        "search_objects",
        search_objects,
        "search_objects(query) -> [{object_name, category, synonym, file}] — find by BUSINESS NAME",
        "discovery",
        ["synonym", "синоним", "бизнес", "search_objects", "объект", "business"],
        "SEARCH BY BUSINESS NAME (requires index v7+):\n"
        "  results = search_objects('себестоимость')\n"
        "  for r in results:\n"
        "      print(r['synonym'], r['category'], r['object_name'])",
    )
    _reg(
        "search_regions",
        search_regions,
        "search_regions(query, limit=200) -> [{name, line, end_line, module_path, object_name, category}]",
        "discovery",
        ["область", "region", "search_regions", "#Область"],
        "FIND CODE REGIONS:\n"
        "  regions = search_regions('Себестоимость')\n"
        "  for r in regions:\n"
        "      print(r['category'], r['object_name'], r['name'], f'L{r[\"line\"]}-{r[\"end_line\"]}')",
    )
    _reg(
        "search_module_headers",
        search_module_headers,
        "search_module_headers(query, limit=200) -> [{module_path, object_name, category, header_comment}]",
        "discovery",
        ["заголовок", "header", "комментарий", "search_module_headers"],
        "FIND MODULES BY HEADER COMMENT:\n"
        "  headers = search_module_headers('себестоимость')\n"
        "  for h in headers:\n"
        "      print(h['category'], h['object_name'], h['header_comment'][:80])",
    )
    _reg(
        "search",
        search,
        "search(query, scope='all', limit=30) -> [{text, source_type, object_name, path, path_kind, detail}]",
        "discovery",
        ["поиск", "search", "найти", "unified", "discovery", "искать"],
        "UNIFIED SEARCH across methods, synonyms, regions, headers:\n"
        "  # Broad first pass:\n"
        "  results = search('себестоимость')\n"
        "  for r in results:\n"
        "      print(r['source_type'], r['text'], r['path'])\n"
        "  # Filter by scope:\n"
        "  search('себестоимость', scope='methods')   # only code methods\n"
        "  search('себестоимость', scope='objects')    # only 1C objects by synonym\n"
        "  search('себестоимость', scope='regions')    # only #Область\n"
        "  search('себестоимость', scope='headers')    # only module headers\n"
        "  # Browse mode (empty query, specific scope, set limit for full list):\n"
        "  search('', scope='objects', limit=20000)  # browse objects (default limit=30)",
    )
    _reg(
        "get_index_info",
        get_index_info,
        "get_index_info() -> {status, builder_version, config_name, has_fts, has_synonyms, ...}",
        "discovery",
        ["index", "version", "индекс", "версия", "info", "get_index_info"],
        "CHECK INDEX CAPABILITIES:\n"
        "  info = get_index_info()\n"
        "  if info.get('status') != 'ok':\n"
        "      print('No index — все хелперы работают через filesystem fallback (медленнее).')\n"
        "      print('USER может построить индекс командой rlm_index(action=\\'build\\') — НЕ вызывай эту команду сам.')\n"
        "  else:\n"
        "      print(f\"Index v{info['builder_version']} ({info['methods']} methods)\")\n"
        "      caps = []\n"
        "      if info.get('has_fts'): caps.append('search_methods')\n"
        "      if info.get('has_synonyms'): caps.append('search_objects')\n"
        "      if info.get('has_regions'): caps.append('search_regions')\n"
        "      if info.get('has_module_headers'): caps.append('search_module_headers')\n"
        "      if info.get('has_form_elements'): caps.append('parse_form')\n"
        "      if info.get('has_object_attributes'): caps.append('find_attributes')\n"
        "      if info.get('has_predefined_items'): caps.append('find_predefined')\n"
        "      if info.get('has_extension_overrides'): caps.append('get_overrides')\n"
        "      print('INSTANT helpers:', caps)",
    )

    _reg(
        "find_http_services",
        find_http_services,
        "find_http_services(name='') -> [{name, root_url, templates}]",
        "business",
        ["http", "сервис", "endpoint", "rest", "api"],
        "FIND HTTP SERVICES:\n"
        "  services = find_http_services()\n"
        "  for s in services:\n"
        "      print(f\"  {s['name']} (/{s['root_url']})\")\n"
        "      for t in s['templates']:\n"
        "          print(f\"    {t['template']}: {[m['http_method'] for m in t['methods']]}\")",
    )
    _reg(
        "find_web_services",
        find_web_services,
        "find_web_services(name='') -> [{name, namespace, operations}]",
        "business",
        ["soap", "wsdl", "веб", "web service", "ws"],
        "FIND WEB SERVICES (SOAP):\n"
        "  services = find_web_services()\n"
        "  for s in services:\n"
        "      print(f\"  {s['name']} ns={s['namespace']}\")\n"
        "      for op in s['operations']:\n"
        "          print(f\"    {op['name']}({', '.join(op['params'])}) -> {op['return_type']}\")",
    )
    _reg(
        "find_xdto_packages",
        find_xdto_packages,
        "find_xdto_packages(name='') -> [{name, namespace, types}]",
        "business",
        ["xdto", "пакет", "namespace", "схема", "тип данных"],
        "FIND XDTO PACKAGES:\n"
        "  pkgs = find_xdto_packages()\n"
        "  for p in pkgs:\n"
        "      print(f\"  {p['name']} ns={p['namespace']} types={len(p.get('types', []))}\")",
    )
    _reg(
        "find_exchange_plan_content",
        find_exchange_plan_content,
        "find_exchange_plan_content(name) -> [{ref, auto_record}]",
        "business",
        ["обмен", "exchange", "план обмена", "синхрониз", "регистрац"],
        "FIND EXCHANGE PLAN CONTENT:\n"
        "  content = find_exchange_plan_content('ОбменУправлениеПредприятием')\n"
        "  for item in content:\n"
        "      print(f\"  {item['ref']} auto_record={item['auto_record']}\")",
    )

    _reg(
        "find_references_to_object",
        find_references_to_object,
        "find_references_to_object(object_ref, kinds=None, limit=1000, include_code=False) -> {object, references: [{used_in, path, line, kind}], total, truncated, partial, by_kind} (+ code_usages/code_total/code_by_kind/code_truncated/code_partial/code_meta when include_code=True)",
        "business",
        [
            "ссылк",
            "references",
            "где используется",
            "найти ссылки",
            "в свойствах",
            "поиск ссылок",
            "вхождения",
        ],
        "FIND REFERENCES TO OBJECT (analogue of Configurator 'Найти ссылки → В свойствах'):\n"
        "  res = find_references_to_object('Справочник.ВидыПодарочныхСертификатов')\n"
        "  print(f\"total={res['total']} by_kind={res['by_kind']}\")\n"
        "  for r in res['references'][:20]:\n"
        "      print(f\"  {r['kind']:25s} {r['used_in']} ({r['path']})\")\n"
        "  # Filter by kind:\n"
        "  attrs_only = find_references_to_object('Справочник.X', kinds=['attribute_type'])\n"
        "  # Metadata refs + in-code usages in one call:\n"
        "  full = find_references_to_object('Документ.X', include_code=True)\n"
        "  print(f\"meta={full['total']} code={full['code_total']} {full['code_by_kind']}\")\n"
        "  # On v11 indexes (no metadata_references table) — partial=True via live scan",
    )

    _reg(
        "find_code_usages",
        find_code_usages,
        "find_code_usages(object_ref, kind=None, limit=1000) -> {object, usages: [{path, object_name, category, module_type, line, kind, member}], by_kind, total, truncated, partial, _meta}",
        "business",
        [
            "использования в коде",
            "где используется в коде",
            "code usages",
            "обращения",
            "find_code_usages",
            "ТЧ в запросах",
        ],
        "FIND CODE USAGES (reverse: where a metadata object is used IN CODE):\n"
        "  res = find_code_usages('Документ.ПриобретениеТоваровУслуг')\n"
        "  print(f\"total={res['total']} by_kind={res['by_kind']}\")\n"
        "  for u in res['usages'][:20]:\n"
        "      tail = f\" .{u['member']}\" if u['member'] else ''\n"
        "      print(f\"  {u['kind']:8s} {u['path']}:{u['line']}{tail}\")\n"
        "  # kind: 'manager' (Документы.X) | 'ref_type' (\"ДокументСсылка.X\") | 'query' (Документ.X.ТЧ)\n"
        "  # Filter: find_code_usages('Документ.X', kind='query')\n"
        "  # Pairs with find_references_to_object (metadata-XML refs). Scope: main config only.",
    )

    _reg(
        "find_defined_types",
        find_defined_types,
        "find_defined_types(name) -> {name, types: list[str], path, partial}",
        "business",
        ["определяемый тип", "defined type", "ОпределяемыйТип"],
        "FIND DEFINED TYPES (раскрытие ОпределяемогоТипа):\n"
        "  dt = find_defined_types('ДенежнаяСуммаНеотрицательная')\n"
        "  print(dt['types'])  # -> ['Number'] or ['Catalog.X', 'Document.Y', ...]",
    )

    _reg(
        "detect_extensions",
        detect_extensions,
        "detect_extensions() -> {config_role, nearby_extensions, nearby_main, warnings}",
        "extension",
        ["обнаружить расширения", "детект", "detect", "extension list"],
        "DETECT EXTENSIONS (диагностика контекста):\n"
        "  ctx = detect_extensions()\n"
        "  print(f\"Роль: {ctx['config_role']}\")  # main / extension / unknown\n"
        "  for e in ctx.get('nearby_extensions', []):\n"
        "      print(f\"  {e.get('name')} (prefix={e.get('prefix')})\")  # ключ 'prefix', не 'name_prefix'\n"
        "  # Дальше: get_overrides() для индексных перехватов или find_ext_overrides(ext_path) live",
    )
    _reg(
        "find_ext_overrides",
        find_ext_overrides,
        "find_ext_overrides(ext_path, obj='') -> {extension_path, object_filter, overrides:[{annotation, target_method, extension_method, ...}], total}",
        "extension",
        ["перехваты расширения", "ext_overrides", "live overrides", "перехваты live"],
        "FIND OVERRIDES IN EXTENSION (live, без индекса):\n"
        "  ctx = detect_extensions()\n"
        "  for e in ctx.get('nearby_extensions', []):\n"
        "      print(f\"  {e.get('name')} -> {e.get('path')}\")\n"
        "      ovr = find_ext_overrides(e['path'])  # все перехваты этого расширения\n"
        "      print(f\"    total={ovr['total']}\")\n"
        "      for o in ovr['overrides'][:5]:\n"
        "          print(f\"      &{o['annotation']} {o['target_method']}\")\n"
        "  # Прицельный поиск по объекту:\n"
        "  ovr_obj = find_ext_overrides(ext_path, 'Номенклатура')\n"
        "  # Если есть индекс v9+ — предпочитай get_overrides() (мгновенно из SQLite).\n"
        "  # find_ext_overrides — для live-проверки на проектах без индекса или для верификации.",
    )
    _reg(
        "get_overrides",
        get_overrides,
        "get_overrides(object_name='', method_name='') -> {overrides: [...], total, source}",
        "extension",
        ["перехват", "override", "расширен", "extension", "вместо", "после", "перед"],
        "GET OVERRIDES:\n"
        "  result = get_overrides('Номенклатура')\n"
        "  for ov in result['overrides']:\n"
        "      print(f\"  {ov['target_method']} <- {ov['annotation']} {ov.get('extension_name', '')}\")\n"
        "  # To read extension method body:\n"
        "  body = read_procedure(path, 'MethodName', include_overrides=True)\n"
        "  # NOTE: extension files are OUTSIDE the sandbox: read_file/grep/glob_files on '../' paths\n"
        "  # raise PermissionError. BUT: high-level BSL helpers (read_procedure, extract_procedures,\n"
        "  # parse_object_xml, find_attributes, find_predefined, search) accept '../' paths returned by\n"
        "  # find_module and read extensions internally.",
    )

    _reg(
        "help",
        bsl_help,
        "help(task='') -> str  # get recipe: help('exports'), help('movements'), help('flow')",
        "navigation",
    )

    # git_search — opt-in full-text backend. Registered only when the sources
    # are under git ("auto", live sessions) or unconditionally for the rlm_help
    # doc snapshot ("force"); never under "never".
    _want_git_search = register_git_search == "force" or (register_git_search == "auto" and _git_search_available())
    if _want_git_search:
        _reg(
            "git_search",
            git_search,
            "git_search(pattern, path='', file_types='', regex=False, ignore_case=False, mode='lines', max_results=200)"
            " -> [{file,line,text}] | [{file}] (mode='files'). FULL-TEXT over ALL files incl. raw XML/forms/queries."
            " Only available when sources are under git.",
            "navigation",
            [
                "полнотекст",
                "поиск везде",
                "grep по всем файлам",
                "найти подстроку",
                "найти строку",
                "найти текст",
                "xml поиск",
                "git_search",
                "git grep",
            ],
            "FULL-TEXT SEARCH — all files, incl. raw XML/forms/rights/DCS/queries (only under git):\n"
            "  hits = git_search('VIN')                       # substring anywhere\n"
            "  hits = git_search('VIN', file_types='xml')     # narrow to a file type\n"
            "  hits = git_search('VIN', path='Catalogs', mode='files')  # overview: which files\n"
            "  for h in hits:\n"
            "      print(h.get('file'), h.get('line'), h.get('text', ''))\n"
            "  # Searches CURRENT on-disk state (incl. uncommitted + new untracked); .gitignore'd skipped.\n"
            "  # Anti-noise on common tokens: start with mode='files' or a narrow file_types/path, then drill down.\n"
            "  # Mind max_results / the {'_truncated': True} sentinel; regex=True is POSIX ERE\n"
            "  #   (end-of-line anchor on CRLF files needs '[[:space:]]*$', not '$').\n"
            "  # On failure returns [{'error': ...}] (NOT []). For a known module use safe_grep instead.",
        )

    # ── Return all helpers (auto-generated from registry) ────────
    return {
        "_detected_prefixes": _ensure_prefixes,
        "_registry": _registry,
        **{k: v["fn"] for k, v in _registry.items()},
    }
