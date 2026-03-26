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


_INIT_URL = "https://kite.zerodha.com/connect/login?api_key=test_api_key&sess_id=init_sess_id"


def _mock_session(request_token="rt_test", access_token="at_final"):
    """Pre-wired mock session for the happy-path login flow."""
    s = MagicMock()

    init_r = MagicMock()
    init_r.url = _INIT_URL
    s.get.return_value = init_r

    login_r = MagicMock()
    login_r.json.return_value = {"status": "success", "data": {"request_id": "req_id"}}

    twofa_r = MagicMock()
    twofa_r.status_code = 302
    twofa_r.headers = {
        "Location": f"https://127.0.0.1/?request_token={request_token}&status=success"
    }

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

    def test_multi_hop_redirect_eventually_finds_token(self):
        """Follows an intermediate redirect before the final URL contains request_token."""
        s = MagicMock()

        login_r = MagicMock()
        login_r.json.return_value = {"status": "success", "data": {"request_id": "r"}}

        twofa_r = MagicMock()
        twofa_r.status_code = 302
        twofa_r.headers = {"Location": "https://kite.zerodha.com/intermediate"}

        hop_r = MagicMock()
        hop_r.status_code = 302
        hop_r.headers = {"Location": "https://127.0.0.1/?request_token=rt_hop&status=success"}
        hop_r.url = _INIT_URL

        token_r = MagicMock()
        token_r.json.return_value = {"status": "success", "data": {"access_token": "at_hop"}}

        s.post.side_effect = [login_r, twofa_r, token_r]
        s.get.return_value = hop_r  # s.get for init + intermediate both return hop_r

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            assert kite_auth.login() == "at_hop"

    def test_bad_credentials_raises(self):
        s = MagicMock()
        s.get.return_value.url = _INIT_URL
        bad_r = MagicMock()
        bad_r.json.return_value = {"status": "error", "message": "Invalid credentials"}
        s.post.return_value = bad_r

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            with pytest.raises(RuntimeError, match="Login failed"):
                kite_auth.login()

    def test_twofa_200_then_retrigger_finds_token(self):
        """When twofa returns 200 (no redirect), re-hitting connect URL gets the token."""
        s = MagicMock()

        login_r = MagicMock()
        login_r.json.return_value = {"status": "success", "data": {"request_id": "r"}}

        twofa_r = MagicMock()
        twofa_r.status_code = 200
        twofa_r.headers = {}  # no Location

        # connect re-hit returns 302 → redirect_url with request_token
        retrigger_r = MagicMock()
        retrigger_r.status_code = 302
        retrigger_r.headers = {
            "Location": "https://127.0.0.1/?request_token=rt_retrig&status=success"
        }

        token_r = MagicMock()
        token_r.json.return_value = {"status": "success", "data": {"access_token": "at_retrig"}}

        retrigger_r.url = "https://127.0.0.1/?request_token=rt_retrig&status=success"
        s.post.side_effect = [login_r, twofa_r, token_r]
        # Both init GET and re-trigger GET return retrigger_r; init URL must have sess_id
        init_r = MagicMock()
        init_r.url = _INIT_URL
        s.get.side_effect = [init_r, retrigger_r]

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            assert kite_auth.login() == "at_retrig"

    def test_no_redirect_at_all_raises(self):
        s = MagicMock()
        login_r = MagicMock()
        login_r.json.return_value = {"status": "success", "data": {"request_id": "r"}}
        twofa_r = MagicMock()
        twofa_r.status_code = 200
        twofa_r.headers = {}

        # re-trigger also returns 200 with no Location
        retrigger_r = MagicMock()
        retrigger_r.status_code = 200
        retrigger_r.headers = {}
        retrigger_r.url = "https://kite.zerodha.com/connect/login?v=3&api_key=test_api_key"

        s.post.side_effect = [login_r, twofa_r]
        init_r = MagicMock()
        init_r.url = _INIT_URL
        s.get.side_effect = [init_r, retrigger_r]

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            with pytest.raises(RuntimeError, match="Could not extract request_token"):
                kite_auth.login()

    def test_authorize_consent_page_posts_and_finds_token(self):
        """When re-trigger lands on /connect/authorize?sess_id=…, POST to it to get token."""
        s = MagicMock()

        login_r = MagicMock()
        login_r.json.return_value = {"status": "success", "data": {"request_id": "r"}}

        twofa_r = MagicMock()
        twofa_r.status_code = 200
        twofa_r.headers = {}

        # connect re-hit lands on the authorize consent page
        authorize_get_r = MagicMock()
        authorize_get_r.url = "https://kite.zerodha.com/connect/authorize?api_key=test_api_key&sess_id=SID123"

        # sess_info GET returns app_id
        sess_info_r = MagicMock()
        sess_info_r.json.return_value = {"status": "success", "data": {"app_id": 123, "redirect_params": ""}}

        # The candidates loop finds request_token on the first POST attempt
        authorize_req_r = MagicMock()
        authorize_req_r.url = "https://127.0.0.1/?request_token=rt_auth&status=success"

        token_r = MagicMock()
        token_r.json.return_value = {"status": "success", "data": {"access_token": "at_auth"}}

        init_r = MagicMock()
        init_r.url = _INIT_URL
        s.get.side_effect = [init_r, authorize_get_r, sess_info_r]
        s.request.return_value = authorize_req_r
        s.post.side_effect = [login_r, twofa_r, token_r]

        with patch("kite_auth.requests.Session", return_value=s), \
             patch("kite_auth.pyotp.TOTP") as mock_totp, \
             patch.dict(os.environ, _ENV):
            mock_totp.return_value.now.return_value = "123456"
            assert kite_auth.login() == "at_auth"

        # Verify the first candidate call hit /api/connect/app/authorize with sess_id
        first_candidate = s.request.call_args_list[0]
        assert first_candidate[0][0] == "POST"
        assert "authorize" in first_candidate[0][1]
        assert first_candidate[1]["data"]["sess_id"] == "SID123"

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
