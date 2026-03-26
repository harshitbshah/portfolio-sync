import os
import pytest
from unittest.mock import MagicMock, patch
import requests as requests_lib

import kite_auth

_ENV = {
    "KITE_API_KEY": "test_api_key",
    "KITE_API_SECRET": "test_api_secret",
    "ZERODHA_USER_ID": "AB1234",
    "ZERODHA_PASSWORD": "hunter2",
    "ZERODHA_TOTP_KEY": "JBSWY3DPEHPK3PXP",
}

# The URL that step 1 lands on (with sess_id)
_LOGIN_URL = "https://kite.zerodha.com/connect/login?api_key=test_api_key&sess_id=SESS123"


def _mock_session(request_token="rt_test", access_token="at_final"):
    """Pre-wired mock session for the happy-path login flow."""
    s = MagicMock()

    # Step 1: init GET (connect_url) — returns a response with a URL containing sess_id
    init_r = MagicMock()
    init_r.url = _LOGIN_URL

    # Step 4: _follow_chain GETs login_url; mock returns final redirect_url directly
    final_r = MagicMock()
    final_r.status_code = 200
    final_r.headers = {"Location": ""}
    final_r.url = f"https://127.0.0.1/?request_token={request_token}&status=success"

    s.get.side_effect = [init_r, final_r]

    login_r = MagicMock()
    login_r.json.return_value = {"status": "success", "data": {"request_id": "req_id"}}

    twofa_r = MagicMock()
    twofa_r.status_code = 200
    twofa_r.url = "https://kite.zerodha.com/api/twofa"
    twofa_r.headers = {}

    token_r = MagicMock()
    token_r.json.return_value = {"status": "success", "data": {"access_token": access_token}}

    s.post.side_effect = [login_r, twofa_r, token_r]
    return s


class TestLogin:
    def test_happy_path_returns_access_token(self):
        s = _mock_session(access_token="the_token")
        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            assert kite_auth.login() == "the_token"

    def test_skip_session_sent_in_twofa_payload(self):
        """skip_session=True must be included in the twofa POST data."""
        s = _mock_session()
        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            kite_auth.login()

        twofa_call = s.post.call_args_list[1]
        assert twofa_call[1]["data"]["skip_session"] in (True, "true", "True")

    def test_totp_value_sent_in_twofa_call(self):
        s = _mock_session()
        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "999888"
            kite_auth.login()

        twofa_call = s.post.call_args_list[1]
        assert twofa_call[1]["data"]["twofa_value"] == "999888"
        assert twofa_call[1]["data"]["twofa_type"] == "totp"

    def test_connection_error_extracts_token_from_url(self):
        """When redirect chain ends at 127.0.0.1, extract token from ConnectionError URL."""
        s = MagicMock()

        init_r = MagicMock()
        init_r.url = _LOGIN_URL
        s.get.side_effect = [
            init_r,
            requests_lib.exceptions.ConnectionError(
                request=MagicMock(url="https://127.0.0.1/?request_token=rt_conn&status=success")
            ),
        ]

        login_r = MagicMock()
        login_r.json.return_value = {"status": "success", "data": {"request_id": "r"}}
        twofa_r = MagicMock()
        twofa_r.status_code = 200
        twofa_r.headers = {}
        token_r = MagicMock()
        token_r.json.return_value = {"status": "success", "data": {"access_token": "at_conn"}}
        s.post.side_effect = [login_r, twofa_r, token_r]

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            assert kite_auth.login() == "at_conn"

    def test_bad_credentials_raises(self):
        s = MagicMock()
        s.get.return_value.url = _LOGIN_URL
        bad_r = MagicMock()
        bad_r.json.return_value = {"status": "error", "message": "Invalid credentials"}
        s.post.return_value = bad_r

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            with pytest.raises(RuntimeError, match="Login failed"):
                kite_auth.login()

    def test_no_request_token_in_redirect_raises(self):
        """If neither step4 URL yields a request_token, raise."""
        s = MagicMock()
        init_r = MagicMock()
        init_r.url = _LOGIN_URL
        no_token_r = MagicMock()
        no_token_r.status_code = 200
        no_token_r.headers = {"Location": ""}
        no_token_r.url = "https://kite.zerodha.com/connect/login?error=something"
        s.get.side_effect = [init_r, no_token_r, no_token_r]

        login_r = MagicMock()
        login_r.json.return_value = {"status": "success", "data": {"request_id": "r"}}
        twofa_r = MagicMock()
        twofa_r.status_code = 200
        twofa_r.headers = {}
        s.post.side_effect = [login_r, twofa_r]

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            with pytest.raises(RuntimeError, match="Could not extract request_token"):
                kite_auth.login()

    def test_session_generation_failure_raises(self):
        s = _mock_session()
        bad_token_r = MagicMock()
        bad_token_r.json.return_value = {"status": "error", "message": "Bad token"}
        login_r, twofa_r, _ = s.post.side_effect
        s.post.side_effect = [login_r, twofa_r, bad_token_r]

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            with pytest.raises(RuntimeError, match="Session generation failed"):
                kite_auth.login()
