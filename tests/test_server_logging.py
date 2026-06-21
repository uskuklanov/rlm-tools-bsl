"""v1.24.0 #8 — narrow asyncio ConnectionResetError [WinError 10054] log filter.

On Windows ProactorEventLoop, benign HTTP-connection teardown raises
ConnectionResetError inside ``_call_connection_lost``; the default asyncio handler
logs it with a traceback, spamming server.log. The filter suppresses ONLY that
benign teardown noise — any other asyncio error must pass through untouched.
"""

from __future__ import annotations

import logging
import sys

from rlm_tools_bsl.server import (
    _AsyncioConnResetFilter,
    _install_asyncio_conn_reset_filter,
)


class _BenignWinReset(ConnectionResetError):
    """Portable stand-in for the Windows teardown error.

    Setting ``.winerror`` directly is non-portable: the 4-arg OSError form only
    populates ``winerror`` on Windows, and the base ``winerror`` getset-descriptor
    is not writable. A plain class attribute resolves ahead of that descriptor in
    the MRO on every platform, so the filter sees ``winerror == 10054`` on Linux/macOS CI too.
    """

    winerror = 10054


def _call_connection_lost(exc):
    # Named exactly like the asyncio teardown callback so the traceback carries it.
    raise exc


def _record_for(exc, msg="Fatal error on transport"):
    try:
        _call_connection_lost(exc)
    except BaseException:  # noqa: BLE001
        exc_info = sys.exc_info()
    return logging.LogRecord("asyncio", logging.ERROR, __file__, 0, msg, None, exc_info)


def test_suppresses_benign_winerror_teardown():
    # winerror=10054 via a portable subclass (see _BenignWinReset) — errno unset.
    exc = _BenignWinReset("An existing connection was forcibly closed")
    assert getattr(exc, "winerror", None) == 10054  # portable across OSes
    rec = _record_for(exc)
    assert _AsyncioConnResetFilter().filter(rec) is False


def test_suppresses_benign_errno_teardown():
    exc = ConnectionResetError(10054, "An existing connection was forcibly closed")
    rec = _record_for(exc)
    assert _AsyncioConnResetFilter().filter(rec) is False


def test_passes_conn_reset_with_other_errno():
    exc = ConnectionResetError(99, "some other reset")
    rec = _record_for(exc)
    assert _AsyncioConnResetFilter().filter(rec) is True


def test_passes_other_asyncio_error():
    # Same teardown frame but a different exception type → must NOT be suppressed.
    exc = ValueError("totally different problem")
    rec = _record_for(exc)
    assert _AsyncioConnResetFilter().filter(rec) is True


def test_passes_conn_reset_without_teardown_frame():
    # Right errno, but not the _call_connection_lost teardown path → keep it.
    exc = ConnectionResetError(10054, "reset elsewhere")
    try:
        raise exc
    except BaseException:  # noqa: BLE001
        exc_info = sys.exc_info()
    rec = logging.LogRecord("asyncio", logging.ERROR, __file__, 0, "msg", None, exc_info)
    assert _AsyncioConnResetFilter().filter(rec) is True


def test_record_without_exc_info_passes():
    rec = logging.LogRecord("asyncio", logging.INFO, __file__, 0, "plain message", None, None)
    assert _AsyncioConnResetFilter().filter(rec) is True


def test_install_is_idempotent():
    asyncio_logger = logging.getLogger("asyncio")
    before = [f for f in asyncio_logger.filters if isinstance(f, _AsyncioConnResetFilter)]
    for f in before:
        asyncio_logger.removeFilter(f)
    try:
        _install_asyncio_conn_reset_filter()
        _install_asyncio_conn_reset_filter()
        ours = [f for f in asyncio_logger.filters if isinstance(f, _AsyncioConnResetFilter)]
        assert len(ours) == 1
    finally:
        for f in [f for f in asyncio_logger.filters if isinstance(f, _AsyncioConnResetFilter)]:
            asyncio_logger.removeFilter(f)
