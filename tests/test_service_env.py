"""Unit tests for pure helpers in rlm_tools_bsl._service_env.

Pure helpers — no pywin32 imports — so these tests run on Linux and Windows
CI without skip (CI installs only the dev group; service group with pywin32
is optional).
"""

from __future__ import annotations

from rlm_tools_bsl._service_env import build_service_env_vars, build_service_pythonpath


def test_build_pythonpath_includes_all_pywin32_dirs() -> None:
    sp = r"C:\Users\u\AppData\Roaming\uv\tools\rlm-tools-bsl\Lib\site-packages"
    result = build_service_pythonpath(sp)
    parts = result.split(";")
    assert parts == [
        sp,
        sp + r"\win32",
        sp + r"\win32\lib",
        sp + r"\Pythonwin",
    ]


def test_build_pythonpath_first_entry_is_site_packages() -> None:
    sp = r"D:\envs\rlm\Lib\site-packages"
    result = build_service_pythonpath(sp)
    assert result.split(";")[0] == sp


def test_build_service_env_vars_pythonpath_uses_helper() -> None:
    sp = r"C:\sp"
    cfg = r"C:\cfg"
    env_vars = build_service_env_vars(sp, cfg)
    assert env_vars[0].startswith(f"PYTHONPATH={sp};")
    assert f";{sp}\\win32;" in env_vars[0]
    assert env_vars[0].endswith(r"\Pythonwin")


def test_build_service_env_vars_includes_config_file() -> None:
    env_vars = build_service_env_vars(r"C:\sp", r"C:\cfg")
    assert env_vars[1] == r"RLM_CONFIG_FILE=C:\cfg"
    assert len(env_vars) == 2
