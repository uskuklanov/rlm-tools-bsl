from __future__ import annotations

import io
import contextlib
import builtins
import functools
import pathlib
import signal
import threading
import time as _time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass

from rlm_tools_bsl.helpers import make_helpers
from rlm_tools_bsl.bsl_helpers import make_bsl_helpers


ALLOWED_MODULES = frozenset(
    {
        "re",
        "json",
        "collections",
        "math",
        "fnmatch",
        "itertools",
        "functools",
        "operator",
        "string",
        "textwrap",
        "difflib",
        "statistics",
    }
)

BLOCKED_BUILTINS = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "__import__",
        "breakpoint",
        "exit",
        "quit",
        "input",
    }
)


@dataclass
class HelperCall:
    name: str
    elapsed: float
    seq: int = 0
    duplicate_of: int | None = None


@dataclass
class ExecutionResult:
    stdout: str
    error: str | None
    variables: list[str]
    helper_calls: list[HelperCall] | None = None


def _make_restricted_import(allowed: frozenset[str]):
    original_import = builtins.__import__

    def restricted_import(name, *args, **kwargs):
        if name not in allowed and name.split(".")[0] not in allowed:
            raise ImportError(f"Import of '{name}' is not allowed in the sandbox")
        return original_import(name, *args, **kwargs)

    return restricted_import


class Sandbox:
    def __init__(
        self,
        base_path: str,
        max_output_chars: int = 15_000,
        execution_timeout_seconds: int = 45,
        format_info=None,
        idx_reader=None,
        idx_zero_callers_authoritative: bool = False,
    ):
        self._base_path = base_path
        self._max_output_chars = max_output_chars
        self._execution_timeout_seconds = execution_timeout_seconds
        self._format_info = format_info
        self._idx_reader = idx_reader
        self._idx_zero_callers_authoritative = idx_zero_callers_authoritative
        self._namespace: dict = {}
        self._resolve_safe = None
        self._helper_calls: list[HelperCall] = []
        # Session-wide state for duplicate-call detection. NOT cleared in execute().
        self._session_call_count: int = 0
        self._session_call_signatures: dict[str, int] = {}
        self._setup_namespace()

    def _setup_namespace(self) -> None:
        safe_builtins = {k: v for k, v in builtins.__dict__.items() if k not in BLOCKED_BUILTINS}
        safe_builtins["__import__"] = _make_restricted_import(ALLOWED_MODULES)

        original_open = builtins.open

        def restricted_open(file, mode="r", *args, **kwargs):
            if any(c in mode for c in "wxa+"):
                raise PermissionError(f"Write access denied in sandbox (mode='{mode}')")

            if self._resolve_safe is None:
                raise RuntimeError("Sandbox path resolver was not initialized")

            if isinstance(file, int):
                raise PermissionError("File descriptor access is not allowed in sandbox")

            # Keep read access scoped to the sandbox root.
            safe_path = self._resolve_safe(str(pathlib.Path(file)))
            return original_open(safe_path, mode, *args, **kwargs)

        safe_builtins["open"] = restricted_open

        self._namespace["__builtins__"] = safe_builtins

        helpers, self._resolve_safe = make_helpers(self._base_path, idx_reader=self._idx_reader)
        self._namespace.update(self._wrap_helpers(helpers))

        if self._format_info is not None:
            bsl_helpers = make_bsl_helpers(
                base_path=self._base_path,
                resolve_safe=self._resolve_safe,
                read_file_fn=helpers["read_file"],
                grep_fn=helpers["grep"],
                glob_files_fn=helpers["glob_files"],
                format_info=self._format_info,
                idx_reader=self._idx_reader,
                idx_zero_callers_authoritative=self._idx_zero_callers_authoritative,
            )
            self._namespace.update(self._wrap_helpers(bsl_helpers))

            # --- Agent-facing line numbering (presentation layer) ---
            from rlm_tools_bsl._format import number_lines

            _raw_rf = helpers["read_file"]

            def _numbered_read_file(path: str) -> str:
                return number_lines(_raw_rf(path))

            def _numbered_read_files(paths: list[str]) -> dict[str, str]:
                result = {}
                for path in paths:
                    try:
                        result[path] = number_lines(_raw_rf(path))
                    except (OSError, PermissionError) as e:
                        result[path] = f"[error: {e}]"
                return result

            _raw_grep_read = helpers["grep_read"]

            def _numbered_grep_read(pattern, path=".", max_files=10, context_lines=0):
                result = _raw_grep_read(pattern, path, max_files, context_lines)
                if context_lines == 0:
                    for fp in list(result.get("files", {})):
                        content = result["files"][fp]
                        if not content.startswith("[error:"):
                            result["files"][fp] = number_lines(content)
                return result

            _raw_read_procedure = bsl_helpers.get("read_procedure")

            def _numbered_read_procedure(path, proc_name, include_overrides=False):
                return _raw_read_procedure(path, proc_name, include_overrides, numbered=True)

            numbered_overrides = [
                ("read_file", _numbered_read_file),
                ("read_files", _numbered_read_files),
                ("grep_read", _numbered_grep_read),
            ]
            if _raw_read_procedure is not None:
                numbered_overrides.append(("read_procedure", _numbered_read_procedure))
            for name, fn in numbered_overrides:
                self._namespace[name] = self._wrap_helpers({name: fn})[name]

    def _wrap_helpers(self, helpers: dict) -> dict:
        """Wrap callable helpers with timing + session-wide duplicate-call detection."""
        wrapped = {}
        for name, obj in helpers.items():
            if callable(obj):

                @functools.wraps(obj)
                def _timed(*args, _fn=obj, _name=name, **kwargs):
                    t0 = _time.monotonic()
                    self._session_call_count += 1
                    seq = self._session_call_count
                    duplicate_of: int | None = None
                    sig_key: str | None = None
                    try:
                        sig_key = repr((_name, args, sorted(kwargs.items())))
                    except Exception:
                        sig_key = None
                    if sig_key is not None:
                        prev = self._session_call_signatures.get(sig_key)
                        if prev is not None:
                            duplicate_of = prev
                        else:
                            self._session_call_signatures[sig_key] = seq
                    try:
                        return _fn(*args, **kwargs)
                    finally:
                        self._helper_calls.append(
                            HelperCall(
                                _name,
                                _time.monotonic() - t0,
                                seq=seq,
                                duplicate_of=duplicate_of,
                            )
                        )

                wrapped[name] = _timed
            else:
                wrapped[name] = obj
        return wrapped

    @contextmanager
    def _execution_timeout(self):
        if self._execution_timeout_seconds <= 0:
            yield
            return

        if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGALRM"):
            # Unix: signal-based timeout (precise, interrupts C extensions)
            def _raise_timeout(_signum, _frame):
                raise TimeoutError(f"Execution timed out after {self._execution_timeout_seconds} seconds")

            previous_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _raise_timeout)
            signal.setitimer(signal.ITIMER_REAL, self._execution_timeout_seconds)
            try:
                yield
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
        else:
            # Windows / non-main thread: threading-based timeout
            # Sets a flag that we check — cannot interrupt blocking I/O,
            # but catches long-running Python loops.
            import ctypes

            timed_out = threading.Event()
            target_tid = threading.current_thread().ident

            def _timeout_watchdog():
                timed_out.set()
                if target_tid is not None:
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(target_tid),
                        ctypes.py_object(TimeoutError),
                    )

            timer = threading.Timer(self._execution_timeout_seconds, _timeout_watchdog)
            timer.daemon = True
            timer.start()
            try:
                yield
            finally:
                timer.cancel()
                if timed_out.is_set():
                    raise TimeoutError(f"Execution timed out after {self._execution_timeout_seconds} seconds")

    def execute(self, code: str) -> ExecutionResult:
        self._helper_calls.clear()
        stdout_capture = io.StringIO()
        error = None

        try:
            with contextlib.redirect_stdout(stdout_capture):
                with self._execution_timeout():
                    exec(code, self._namespace)
        except Exception:
            error = traceback.format_exc()
            error = self._add_error_hints(error, code)

        stdout = stdout_capture.getvalue()
        if len(stdout) > self._max_output_chars:
            stdout = stdout[: self._max_output_chars] + "\n... [output truncated]"

        return ExecutionResult(
            stdout=stdout,
            error=error,
            variables=self.list_variables(),
            helper_calls=list(self._helper_calls),
        )

    @staticmethod
    def _add_error_hints(error: str, code: str) -> str:
        """Append actionable hints to common errors."""
        hints: list[str] = []

        if "FileNotFoundError" in error or "No such file" in error:
            if "parse_object_xml" in code:
                hints.append(
                    "HINT: parse_object_xml auto-resolves directory paths and 'fake' .mdo/.xml paths. "
                    "Call parse_object_xml('Documents/Name') — it tries Documents/Name/Name.mdo (EDT) "
                    "and Documents/Name/Ext/Document.xml (CF) automatically."
                )
            if "read_procedure" in code:
                hints.append(
                    "HINT: read_procedure raised FileNotFoundError. Possible causes: "
                    "(1) wrong path — use find_module(name) to discover modules; "
                    "(2) object has only XML metadata (e.g. КОДСобытия, ДействияСобытия) — no BSL file. "
                    "If the path is correct but the procedure name might be wrong, call "
                    "extract_procedures(path) for the actual list (with case)."
                )
            if "parse_object_xml" not in code and "read_procedure" not in code and (".xml" in code or ".bsl" in code):
                hints.append(
                    "HINT: Use find_module('Name') or glob_files('**/pattern') to discover correct file paths first."
                )

        if "TimeoutError" in error:
            hints.append(
                "HINT: Operation timed out. For large configs, avoid composite helpers "
                "(analyze_document_flow, analyze_object) and call individual helpers instead: "
                "find_register_movements, find_event_subscriptions, find_callers_context."
            )

        if "NameError" in error:
            hints.append("HINT: Call help() to see available functions. Variables persist between rlm_execute calls.")

        if "import" in error.lower() and "restricted" in error.lower():
            hints.append(
                "HINT: Only standard library modules are allowed. Use built-in helpers instead of external libraries."
            )

        if hints:
            error = error.rstrip() + "\n\n" + "\n".join(hints)

        return error

    def list_variables(self) -> list[str]:
        return [k for k in self._namespace if not k.startswith("_") and k != "__builtins__"]
