"""Pure helpers for Windows service environment configuration.

Kept in a separate module (free of pywin32 imports) so unit tests can run
on any platform — including CI, which installs only the dev group and not
the service group.
"""

from __future__ import annotations

import pathlib


def build_service_pythonpath(site_packages: str) -> str:
    """Build PYTHONPATH for pythonservice.exe in an isolated uv tool env.

    Mirrors what pywin32.pth + pywin32_bootstrap normally adds via site.py —
    site processing does not run for pythonservice.exe in a uv tool env (no
    python.exe sibling, no pyvenv.cfg lookup), so we bake the win32/Pythonwin
    sub-dirs into PYTHONPATH explicitly.

    Uses PureWindowsPath so the result has backslash separators on any
    platform (tests run on Linux CI too).
    """
    sp = pathlib.PureWindowsPath(site_packages)
    parts = [
        site_packages,
        str(sp / "win32"),
        str(sp / "win32" / "lib"),
        str(sp / "Pythonwin"),
    ]
    return ";".join(parts)


def build_service_env_vars(site_packages: str, config_file: str) -> list[str]:
    """Build the REG_MULTI_SZ value list written to the service Environment key."""
    return [
        f"PYTHONPATH={build_service_pythonpath(site_packages)}",
        f"RLM_CONFIG_FILE={config_file}",
    ]
