import sys
from pathlib import Path

# Ensure tests/ is on sys.path so bare imports work on all platforms
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from types import SimpleNamespace

from test_bsl_helpers import _make_bsl_fixture


def pytest_configure(config):
    # Register custom marker so `pytest --strict-markers` does not warn.
    config.addinivalue_line(
        "markers",
        "strategy_mode_slim: run test under RLM_STRATEGY_MODE=slim (default in tests is 'full' for back-compat)",
    )


@pytest.fixture(autouse=True)
def _strategy_mode_default(request, monkeypatch):
    """Pin RLM_STRATEGY_MODE for every test based on marker presence.

    Default in tests is ``full`` so existing assertions ("== HELPERS ==",
    "Step 0 — UNDERSTAND", etc.) keep matching. Tests that need slim opt-in
    via ``@pytest.mark.strategy_mode_slim``.

    The fixture sets the env explicitly in BOTH directions — without this,
    a stray ``RLM_STRATEGY_MODE=slim`` in the developer's shell would silently
    flip every legacy assertion to slim and hide regressions.
    """
    mode = "slim" if "strategy_mode_slim" in request.keywords else "full"
    monkeypatch.setenv("RLM_STRATEGY_MODE", mode)


@pytest.fixture(autouse=True)
def _isolate_ext_display_env(monkeypatch):
    """Снять переменные, управляющие агент-facing представлением расширений.

    Существующие тесты ассертят ПОЛНЫЕ списки расширений (напр.
    ``test_strategy_ext_budget.py`` ждёт все 14 ``Расш0..Расш13``). Стрэй
    ``RLM_EXT_LIST_CAP`` в шелле разработчика/CI урезал бы их и уронил тест на
    нерелевантной причине. Снимаем обе переменные перед каждым тестом по образцу
    ``_strategy_mode_default``; тесты, которым нужно конкретное значение,
    ставят его локальным ``monkeypatch.setenv`` поверх этой autouse-фикстуры.
    """
    monkeypatch.delenv("RLM_EXT_LIST_CAP", raising=False)
    monkeypatch.delenv("RLM_EXT_OVERRIDE_DETAIL", raising=False)


@pytest.fixture(autouse=True)
def _isolate_real_home(tmp_path_factory, monkeypatch):
    """Default-isolation: every test writes indexes AND file-cache to tmp dirs.

    Without this:
    - ``IndexBuilder.build()`` without a test-local ``RLM_INDEX_DIR`` patch
      writes a ~360 KiB ``bsl_index.db`` into the developer's real
      ``~/.cache/rlm-tools-bsl/<hash>/``.
    - ``rlm_start`` / cache helpers without a test-local ``RLM_CONFIG_FILE``
      patch resolve ``cache._cache_base()`` to ``Path.home()/.cache/...`` and
      drop ``file_index.json`` files there.

    Found in v1.9.2 smoke test: a single ``pytest -q`` run accumulated 19
    stale ``bsl_index.db`` and 87 stale ``file_index.json`` in real home.

    Two-layer isolation:
    1. Set ``RLM_INDEX_DIR`` → indexes go to a session-shared tmp dir.
    2. Patch ``pathlib.Path.home`` → any code that falls back to
       ``Path.home()/.cache/...`` (cache module, migration helper) sees a
       fake home instead of the developer's real one.

    Tests that explicitly verify fallback behavior (migration tests, cache
    tests) can still override either layer — monkeypatch applies later
    changes on top of this autouse setup.
    """
    import pathlib

    isolated_root = tmp_path_factory.mktemp("rlm_index_root")
    fake_home = tmp_path_factory.mktemp("rlm_fake_home")
    monkeypatch.setenv("RLM_INDEX_DIR", str(isolated_root))
    monkeypatch.setattr(pathlib.Path, "home", lambda: fake_home)


@pytest.fixture
def bsl_env(tmp_path):
    """Shared BSL test environment with default CF fixture.

    Returns SimpleNamespace with:
        path  – tmp_path (pathlib.Path) where the CF structure lives
        bsl   – dict of BSL helper functions
        helpers – dict of generic helper functions
    """
    tmpdir = str(tmp_path)
    bsl, helpers = _make_bsl_fixture(tmpdir)
    return SimpleNamespace(path=tmp_path, bsl=bsl, helpers=helpers)
