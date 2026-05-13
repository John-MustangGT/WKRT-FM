"""
Tests for admin authentication in wkrt/web.py.
Covers:
  - constant-time comparison (secrets.compare_digest)
  - correct credentials accepted
  - wrong credentials rejected (401)
  - rate-limit lockout (429)
  - no-password mode (always allowed)
"""
import sys
import base64
import importlib
import io
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# We test the internal _Handler class and helpers directly.
import wkrt.web as web_module
from wkrt.web import _Handler, _check_rate_limit, _record_auth_failure, _auth_failures, _auth_lock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_handler(password: str, client_ip: str = "127.0.0.1") -> _Handler:
    """Return a minimally-wired _Handler with a mocked socket and response buffers."""
    _Handler._admin_password = password
    handler = _Handler.__new__(_Handler)
    handler.client_address = (client_ip, 9999)
    handler.headers = {}
    handler._response_code = None
    handler._response_headers = {}
    handler._response_body = b""

    buf = io.BytesIO()
    handler.wfile = buf

    def _send_response(code):
        handler._response_code = code
    def _send_header(name, value):
        handler._response_headers[name] = value
    def _end_headers():
        pass
    def _wfile_write(data):
        handler._response_body += data

    handler.send_response  = _send_response
    handler.send_header    = _send_header
    handler.end_headers    = _end_headers
    handler.wfile.write    = _wfile_write

    return handler


def _basic_auth_header(password: str, username: str = "admin") -> str:
    raw = f"{username}:{password}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def _clear_rate_limits():
    with _auth_lock:
        _auth_failures.clear()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRequireAdmin:
    def setup_method(self):
        _clear_rate_limits()

    def test_no_password_always_allowed(self):
        handler = _make_handler(password="")
        handler.headers = {}
        assert handler._require_admin() is True

    def test_correct_password_accepted(self):
        handler = _make_handler(password="secret")
        handler.headers = {"Authorization": _basic_auth_header("secret")}
        assert handler._require_admin() is True

    def test_wrong_password_rejected(self):
        handler = _make_handler(password="secret")
        handler.headers = {"Authorization": _basic_auth_header("wrong")}
        assert handler._require_admin() is False
        assert handler._response_code == 401

    def test_no_auth_header_rejected(self):
        handler = _make_handler(password="secret")
        handler.headers = {}
        assert handler._require_admin() is False
        assert handler._response_code == 401

    def test_malformed_base64_rejected(self):
        handler = _make_handler(password="secret")
        handler.headers = {"Authorization": "Basic !!!notbase64!!!"}
        assert handler._require_admin() is False
        assert handler._response_code == 401

    def test_uses_secrets_compare_digest(self):
        """Verify secrets.compare_digest is called (not plain ==)."""
        handler = _make_handler(password="secret")
        handler.headers = {"Authorization": _basic_auth_header("secret")}
        with patch("wkrt.web.secrets.compare_digest", return_value=True) as mock_cd:
            result = handler._require_admin()
        mock_cd.assert_called_once()
        assert result is True


class TestRateLimit:
    def setup_method(self):
        _clear_rate_limits()

    def test_first_failures_allowed(self):
        ip = "10.0.0.1"
        for _ in range(web_module._AUTH_MAX_FAILS - 1):
            assert _check_rate_limit(ip) is True
            _record_auth_failure(ip)

    def test_rate_limited_after_max_failures(self):
        ip = "10.0.0.2"
        for _ in range(web_module._AUTH_MAX_FAILS):
            _record_auth_failure(ip)
        assert _check_rate_limit(ip) is False

    def test_rate_limit_returns_429(self):
        ip = "10.0.0.3"
        handler = _make_handler(password="secret", client_ip=ip)
        handler.headers = {"Authorization": _basic_auth_header("wrong")}
        # Fill up the failure bucket
        for _ in range(web_module._AUTH_MAX_FAILS):
            _record_auth_failure(ip)
        result = handler._require_admin()
        assert result is False
        assert handler._response_code == 429

    def test_old_failures_expire(self):
        ip = "10.0.0.4"
        # Inject failures with timestamps old enough to be outside the window
        old_ts = time.monotonic() - web_module._AUTH_WINDOW_SECS - 1
        with _auth_lock:
            _auth_failures[ip] = [old_ts] * web_module._AUTH_MAX_FAILS
        # Should be allowed again because all timestamps are stale
        assert _check_rate_limit(ip) is True

    def test_different_ips_independent(self):
        _clear_rate_limits()
        for _ in range(web_module._AUTH_MAX_FAILS):
            _record_auth_failure("192.168.1.1")
        assert _check_rate_limit("192.168.1.2") is True


class TestWebServerWarning:
    def test_warns_when_no_password(self, caplog):
        import logging
        from wkrt.web import WebServer
        state = MagicMock()
        with caplog.at_level(logging.WARNING, logger="wkrt.web"):
            ws = WebServer(state=state, port=0, admin_password="")
        assert any("admin_password" in r.message.lower() or "wkrt_admin_password" in r.message.lower()
                   for r in caplog.records)

    def test_no_warning_when_password_set(self, caplog):
        import logging
        from wkrt.web import WebServer
        state = MagicMock()
        with caplog.at_level(logging.WARNING, logger="wkrt.web"):
            ws = WebServer(state=state, port=0, admin_password="s3cr3t")
        assert not any("admin_password" in r.message.lower() or "wkrt_admin_password" in r.message.lower()
                       for r in caplog.records)
