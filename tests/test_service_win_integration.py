"""Regression test for `_set_service_environment` in `_service_win.py`.

Guards against a developer "simplifying" the function by inlining the env_vars
list again instead of delegating to `build_service_env_vars` — which is
exactly the path that produced the broken v1.9.3 PYTHONPATH (issue #13).

The test stubs the bare minimum `win32*` + `winreg` modules in `sys.modules`
so `import rlm_tools_bsl._service_win` succeeds on Linux CI without pywin32.
"""

from __future__ import annotations

import sys
import types


def _install_stubs(monkeypatch):
    """Stub the bare minimum so `import rlm_tools_bsl._service_win` works on Linux.

    Notes:
      - DO NOT stub `socket` — `_service_win` imports `urllib.request`, which
        needs the real stdlib socket module.
      - `win32serviceutil.ServiceFramework` must be a class (used as a base
        class for `RlmWindowsService` at module import time).
      - `servicemanager` is NOT imported by `_service_win` (the
        pythonservice.exe bootstrap problem we're fixing is on the
        pythonservice side, not ours), so no stub needed for it.
    """
    win32service = types.ModuleType("win32service")
    win32event = types.ModuleType("win32event")
    win32serviceutil = types.ModuleType("win32serviceutil")
    win32serviceutil.ServiceFramework = object
    monkeypatch.setitem(sys.modules, "win32service", win32service)
    monkeypatch.setitem(sys.modules, "win32event", win32event)
    monkeypatch.setitem(sys.modules, "win32serviceutil", win32serviceutil)

    captured: dict = {}
    fake_winreg = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = 0
    fake_winreg.KEY_SET_VALUE = 0
    fake_winreg.REG_MULTI_SZ = 7

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_winreg.OpenKeyEx = lambda *a, **kw: _Key()
    fake_winreg.SetValueEx = lambda key, name, _r, typ, val: captured.update(name=name, type=typ, value=val)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    return captured


def test_set_service_environment_uses_build_helper(monkeypatch, request):
    captured = _install_stubs(monkeypatch)

    sys.modules.pop("rlm_tools_bsl._service_win", None)
    import rlm_tools_bsl

    if hasattr(rlm_tools_bsl, "_service_win"):
        delattr(rlm_tools_bsl, "_service_win")

    def _cleanup():
        sys.modules.pop("rlm_tools_bsl._service_win", None)
        if hasattr(rlm_tools_bsl, "_service_win"):
            delattr(rlm_tools_bsl, "_service_win")

    request.addfinalizer(_cleanup)

    from rlm_tools_bsl import _service_win
    from rlm_tools_bsl._service_env import build_service_env_vars

    _service_win._set_service_environment("svc-X", r"C:\sp", r"C:\cfg")

    assert captured["name"] == "Environment"
    assert captured["type"] == 7
    assert captured["value"] == build_service_env_vars(r"C:\sp", r"C:\cfg")
