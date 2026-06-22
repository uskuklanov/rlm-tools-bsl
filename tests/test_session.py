import tempfile
import time

import pytest
from rlm_tools_bsl.session import SessionManager, build_session_manager_from_env


def test_create_session():
    manager = SessionManager(max_sessions=5)
    session_id = manager.create(path=tempfile.gettempdir(), query="test query")
    assert session_id is not None
    assert manager.get(session_id) is not None


def test_max_sessions_enforced():
    manager = SessionManager(max_sessions=2)
    manager.create(path=tempfile.gettempdir(), query="q1")
    manager.create(path=tempfile.gettempdir(), query="q2")
    with pytest.raises(RuntimeError, match="max sessions"):
        manager.create(path=tempfile.gettempdir(), query="q3")


def test_end_session():
    manager = SessionManager(max_sessions=5)
    session_id = manager.create(path=tempfile.gettempdir(), query="test")
    manager.end(session_id)
    assert manager.get(session_id) is None


# ---------------------------------------------------------------------------
# Two-tier TTL tests
# ---------------------------------------------------------------------------


def test_idle_session_evicted():
    """Session with 0 execute_calls is evicted by idle timeout."""
    manager = SessionManager(timeout_idle_minutes=0, timeout_active_minutes=10)
    sid = manager.create(path=tempfile.gettempdir(), query="q")
    # Force last_used into the past
    manager._sessions[sid].last_used = time.time() - 1
    evicted = manager.cleanup_expired()
    assert sid in evicted
    assert manager.get(sid) is None


def test_active_session_survives_idle_ttl():
    """Session with execute_calls>0 uses active timeout, survives idle TTL."""
    manager = SessionManager(timeout_idle_minutes=0, timeout_active_minutes=10)
    sid = manager.create(path=tempfile.gettempdir(), query="q")
    manager._sessions[sid].execute_calls = 1
    manager._sessions[sid].last_used = time.time() - 1
    evicted = manager.cleanup_expired()
    assert sid not in evicted
    assert manager.get(sid) is not None


def test_backward_compat_single_timeout():
    """timeout_minutes overrides both idle and active."""
    manager = SessionManager(timeout_minutes=0)
    sid = manager.create(path=tempfile.gettempdir(), query="q")
    manager._sessions[sid].execute_calls = 5
    manager._sessions[sid].last_used = time.time() - 1
    evicted = manager.cleanup_expired()
    assert sid in evicted


def test_build_session_manager_from_env_idle(monkeypatch):
    """RLM_SESSION_TIMEOUT_IDLE env is respected."""
    monkeypatch.setenv("RLM_SESSION_TIMEOUT_IDLE", "5")
    monkeypatch.delenv("RLM_SESSION_TIMEOUT", raising=False)
    sm = build_session_manager_from_env()
    assert sm._timeout_idle == 300


def test_build_session_manager_from_env_backward_compat(monkeypatch):
    """RLM_SESSION_TIMEOUT overrides both idle and active."""
    monkeypatch.setenv("RLM_SESSION_TIMEOUT", "7")
    sm = build_session_manager_from_env()
    assert sm._timeout_idle == 420
    assert sm._timeout_active == 420


def test_on_evict_fires_for_session_evicted_inside_get():
    seen = []
    m = SessionManager(timeout_idle_minutes=0, timeout_active_minutes=10)
    m.on_evict = lambda sid: seen.append(sid)
    sid = m.create(path=tempfile.gettempdir(), query="q")
    m._sessions[sid].last_used = time.time() - 1
    m.get("nonexistent")
    assert sid in seen


def test_on_evict_fires_for_session_evicted_inside_create():
    seen = []
    m = SessionManager(max_sessions=5, timeout_idle_minutes=0, timeout_active_minutes=10)
    m.on_evict = lambda sid: seen.append(sid)
    sid = m.create(path=tempfile.gettempdir(), query="q")
    m._sessions[sid].last_used = time.time() - 1
    m.create(path=tempfile.gettempdir(), query="q2")
    assert sid in seen
