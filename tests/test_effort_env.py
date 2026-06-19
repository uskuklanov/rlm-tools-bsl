"""Часть 2: выбор effort/лимитов (resolve_session_limits + _auto_effort)."""

from __future__ import annotations

import json
import tempfile

import pytest

from rlm_tools_bsl.server import _rlm_start, _rlm_end, resolve_session_limits
from rlm_tools_bsl.bsl_knowledge import EFFORT_LEVELS, _auto_effort


def _clear(monkeypatch):
    for v in ("RLM_FORCE_EFFORT", "RLM_MAX_EXECUTE_CALLS", "RLM_MAX_LLM_CALLS"):
        monkeypatch.delenv(v, raising=False)


# ── _auto_effort: эвристика по тексту запроса ───────────────────────────────


@pytest.mark.parametrize(
    "q",
    [
        "полный жизненный цикл РеализацияТоваровУслуг",
        "как устроен механизм скидок и какие регистры он затрагивает",
        "сквозной сценарий продажи: заказ, резерв, реализация, оплата",
    ],
)
def test_auto_effort_high_on_complex(q):
    assert _auto_effort(q) == "high"


@pytest.mark.parametrize(
    "q",
    [
        "какие реквизиты у РеализацияТоваровУслуг",
        "прочитай процедуру ПередЗаписью",
        "найди модуль ПродажиСервер",
        # negative: общие слова «полный/полное/целиком» НЕ должны поднимать до high
        "полный список реквизитов документа",
        "полный текст процедуры ОбработкаПроведения",
        "полное имя объекта метаданных",
        "прочитай процедуру целиком",
        "",
    ],
)
def test_auto_effort_medium_on_simple(q):
    assert _auto_effort(q) == "medium"


# ── resolve_session_limits: выбор тира ──────────────────────────────────────


def test_auto_default_medium_on_simple_query(monkeypatch):
    _clear(monkeypatch)
    effort, _llm, exec_ = resolve_session_limits("auto", "какие реквизиты у документа", None, None)
    assert effort == "medium"
    assert exec_ == EFFORT_LEVELS["medium"].max_execute_calls  # 25


def test_auto_escalates_high_on_complex_query(monkeypatch):
    _clear(monkeypatch)
    effort, _llm, exec_ = resolve_session_limits("auto", "полный жизненный цикл документа", None, None)
    assert effort == "high"
    assert exec_ == EFFORT_LEVELS["high"].max_execute_calls  # 50


def test_explicit_agent_effort_wins_over_auto(monkeypatch):
    """Агент явно задал тир — уважаем, даже если эвристика сказала бы иначе."""
    _clear(monkeypatch)
    effort, _llm, _exec = resolve_session_limits("low", "полный жизненный цикл", None, None)
    assert effort == "low"


def test_force_effort_env_overrides_everything(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("RLM_FORCE_EFFORT", "medium")
    # сложный запрос + auto → эвристика дала бы high, но env-замок = medium
    effort, _llm, exec_ = resolve_session_limits("auto", "полный жизненный цикл", None, None)
    assert effort == "medium"
    assert exec_ == EFFORT_LEVELS["medium"].max_execute_calls  # 25


def test_force_effort_invalid_ignored_falls_to_auto(monkeypatch):
    """Невалидный RLM_FORCE_EFFORT игнорируется → обычный выбор (auto по запросу)."""
    _clear(monkeypatch)
    monkeypatch.setenv("RLM_FORCE_EFFORT", "bogus")
    effort, _llm, _exec = resolve_session_limits("auto", "какие реквизиты", None, None)
    assert effort == "medium"  # bogus игнор → auto на простом запросе → medium


def test_invalid_agent_effort_falls_back_to_medium(monkeypatch):
    _clear(monkeypatch)
    effort, _llm, _exec = resolve_session_limits("bogus", "полный жизненный цикл", None, None)
    assert effort == "medium"  # невалид → medium (без эскалации)


# ── числовые лимиты ─────────────────────────────────────────────────────────


def test_env_sets_default_call_limit(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("RLM_MAX_EXECUTE_CALLS", "120")
    _e, _llm, exec_ = resolve_session_limits("high", "q", None, None)
    assert exec_ == 120


def test_explicit_param_wins_over_env(monkeypatch):
    """Валидный явный параметр тула честно перекрывает env."""
    _clear(monkeypatch)
    monkeypatch.setenv("RLM_MAX_EXECUTE_CALLS", "120")
    _e, _llm, exec_ = resolve_session_limits("high", "q", None, 80)
    assert exec_ == 80


@pytest.mark.parametrize("bad", [0, -1, -50])
def test_invalid_explicit_max_ignored_falls_to_preset(monkeypatch, bad):
    """round-10/11: невалидный explicit (≤0) НЕ создаёт мёртвую сессию — откат к пресету,
    для ОБОИХ лимитов (max_execute_calls И max_llm_calls)."""
    _clear(monkeypatch)
    # сигнатура: resolve_session_limits(effort, query, max_llm_calls, max_execute_calls)
    _e, _llm, exec_ = resolve_session_limits("high", "q", None, bad)  # bad → max_execute_calls
    assert exec_ == EFFORT_LEVELS["high"].max_execute_calls  # 50, не 0/-1
    _e2, llm, _exec2 = resolve_session_limits("high", "q", bad, None)  # bad → max_llm_calls
    assert llm == EFFORT_LEVELS["high"].max_llm_calls  # 30, не 0/-1


def test_max_llm_calls_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("RLM_MAX_LLM_CALLS", "7")
    _e, llm, _exec = resolve_session_limits("high", "q", None, None)
    assert llm == 7


@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_invalid_numeric_env_ignored(monkeypatch, bad):
    _clear(monkeypatch)
    monkeypatch.setenv("RLM_MAX_EXECUTE_CALLS", bad)
    _e, _llm, exec_ = resolve_session_limits("high", "q", None, None)
    assert exec_ == EFFORT_LEVELS["high"].max_execute_calls  # откат к пресету


# ── Task 2.2: интеграция через _rlm_start ───────────────────────────────────


def test_rlm_start_env_default_in_limits_and_banner(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("RLM_MAX_EXECUTE_CALLS", "12")
    with tempfile.TemporaryDirectory() as tmpdir:
        resp = json.loads(_rlm_start(path=tmpdir, query="дымовой тест", effort="high"))
        assert "error" not in resp, resp
        sid = resp["session_id"]
        try:
            assert resp["limits"]["max_execute_calls"] == 12
            assert resp["effective_effort"] == "high"
            # Баннер правдив: числовой лимит ≠ пресету high → баннер есть.
            assert "SERVER LIMIT OVERRIDE" in resp["strategy"]
            assert "max_execute_calls=12" in resp["strategy"]
        finally:
            _rlm_end(sid)


def test_rlm_start_force_effort_changes_limits_no_banner(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("RLM_FORCE_EFFORT", "low")
    with tempfile.TemporaryDirectory() as tmpdir:
        resp = json.loads(_rlm_start(path=tmpdir, query="дымовой тест", effort="high"))
        sid = resp["session_id"]
        try:
            assert resp["limits"]["max_execute_calls"] == EFFORT_LEVELS["low"].max_execute_calls  # 10
            assert resp["effective_effort"] == "low"
            # Тир сменился целиком → лимиты == пресету low → баннера НЕТ.
            assert "SERVER LIMIT OVERRIDE" not in resp["strategy"]
        finally:
            _rlm_end(sid)


def test_rlm_start_auto_simple_query_is_medium(monkeypatch):
    """Дефолт effort='auto' + простой запрос → medium."""
    _clear(monkeypatch)
    with tempfile.TemporaryDirectory() as tmpdir:
        resp = json.loads(_rlm_start(path=tmpdir, query="какие реквизиты у документа"))
        sid = resp["session_id"]
        try:
            assert resp["effective_effort"] == "medium"
            assert resp["limits"]["max_execute_calls"] == EFFORT_LEVELS["medium"].max_execute_calls  # 25
        finally:
            _rlm_end(sid)


def test_rlm_start_auto_complex_query_is_high(monkeypatch):
    """Дефолт effort='auto' + сложный запрос → high (авто-эскалация)."""
    _clear(monkeypatch)
    with tempfile.TemporaryDirectory() as tmpdir:
        resp = json.loads(_rlm_start(path=tmpdir, query="полный жизненный цикл документа и движения регистров"))
        sid = resp["session_id"]
        try:
            assert resp["effective_effort"] == "high"
            assert resp["limits"]["max_execute_calls"] == EFFORT_LEVELS["high"].max_execute_calls  # 50
        finally:
            _rlm_end(sid)
