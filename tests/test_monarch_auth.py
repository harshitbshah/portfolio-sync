"""Monarch cookie expiry: failure marker, cookie-age reminder, email wiring, refresh script."""

import io
import os
import subprocess
import sys
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

import format_email as fe
import monarch_auth as ma
import sync
import sync_us_portfolio as sup
import update_monarch_cookie as umc

TODAY = date(2026, 11, 20)
REPO_ROOT = Path(__file__).resolve().parent.parent
COOKIE = "session_id=abc; csrftoken=xyz"


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.monarch.com/graphql", code, "err", {}, io.BytesIO(b""))


def _days_ago(n: int) -> str:
    return date.fromordinal(TODAY.toordinal() - n).isoformat()


# ── cookie_status: age thresholds ─────────────────────────────────────────────

@pytest.mark.parametrize("age,state,warns", [
    (0, "ok", False),
    (1, "ok", False),
    (49, "ok", False),
    (50, "expiring", True),   # first warning day
    (59, "expiring", True),
    (60, "overdue", True),    # typical lifetime reached
    (200, "overdue", True),
])
def test_cookie_status_thresholds(age, state, warns):
    status, warning = ma.cookie_status(_days_ago(age), TODAY)
    assert status.startswith(f"{ma.STATUS_PREFIX} {state} — {age} ")
    assert (warning is not None) == warns
    if warns:
        assert f"{age} days old" in warning
        assert "update_monarch_cookie.py" in warning and "login_setup.py" in warning


def test_cookie_status_singular_day():
    status, _ = ma.cookie_status(_days_ago(1), TODAY)
    assert "1 day old" in status and "1 days" not in status


@pytest.mark.parametrize("set_at", [None, "", "   "])
def test_cookie_status_unset_is_silent(set_at):
    assert ma.cookie_status(set_at, TODAY) == (None, None)


@pytest.mark.parametrize("set_at", ["not-a-date", "2026-13-01", "2026/11/20", "11/20/2026"])
def test_cookie_status_malformed_date_warns(set_at):
    status, warning = ma.cookie_status(set_at, TODAY)
    assert "unknown" in status
    assert "not a valid date" in warning


def test_cookie_status_future_date_warns():
    status, warning = ma.cookie_status("2026-12-25", TODAY)
    assert "unknown" in status
    assert "in the future" in warning


@pytest.mark.parametrize("set_at", ["2026-11-20T12:29:15Z", " 2026-11-20 ", "2026-11-20T00:00:00+05:30"])
def test_cookie_status_accepts_datetime_and_whitespace(set_at):
    status, warning = ma.cookie_status(set_at, TODAY)
    assert "ok — 0 days old" in status and warning is None


# ── report_auth_failure ───────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [401, 403])
def test_report_auth_failure_prints_marker(code, capsys):
    assert ma.report_auth_failure(_http_error(code)) is True
    out = capsys.readouterr().out
    assert ma.AUTH_MARKER in out and f"HTTP {code}" in out
    assert f"{ma.STATUS_PREFIX} expired" in out


@pytest.mark.parametrize("err", [_http_error(500), _http_error(502), _http_error(404), ValueError("x")])
def test_report_auth_failure_ignores_other_errors(err, capsys):
    assert ma.report_auth_failure(err) is False
    assert capsys.readouterr().out == ""


# ── sync.py monarch_request ───────────────────────────────────────────────────

@pytest.mark.parametrize("code", [401, 403])
def test_sync_monarch_request_auth_error_marks_and_raises_clear_error(code, capsys):
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(code)):
        with pytest.raises(RuntimeError, match=f"MONARCH_COOKIE expired or invalid.*HTTP {code}") as exc:
            sync.monarch_request(COOKIE, b"{}")
    assert isinstance(exc.value.__cause__, urllib.error.HTTPError)
    assert capsys.readouterr().out.count(ma.AUTH_MARKER) == 1


def test_sync_monarch_request_other_http_error_passes_through(capsys):
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
        with pytest.raises(urllib.error.HTTPError):
            sync.monarch_request(COOKIE, b"{}")
    assert ma.AUTH_MARKER not in capsys.readouterr().out


def test_sync_monarch_request_success_prints_nothing(capsys):
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = b'{"data": {}}'
    with mock.patch("urllib.request.urlopen", return_value=resp):
        assert sync.monarch_request(COOKIE, b"{}") == {"data": {}}
    assert capsys.readouterr().out == ""


# ── sync_us_portfolio._monarch_request (has 5xx retry loop) ───────────────────

def test_us_401_marks_once_without_retry(capsys):
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(401)) as urlopen, \
         mock.patch("time.sleep") as sleep:
        with pytest.raises(urllib.error.HTTPError):
            sup._monarch_request(COOKIE, b"{}")
    assert urlopen.call_count == 1
    sleep.assert_not_called()
    assert capsys.readouterr().out.count(ma.AUTH_MARKER) == 1


def test_us_502_then_401_retries_then_marks(capsys):
    with mock.patch("urllib.request.urlopen", side_effect=[_http_error(502), _http_error(401)]) as urlopen, \
         mock.patch("time.sleep"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            sup._monarch_request(COOKIE, b"{}")
    assert exc.value.code == 401 and urlopen.call_count == 2
    assert ma.AUTH_MARKER in capsys.readouterr().out


def test_us_persistent_502_is_not_an_auth_failure(capsys):
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(502)) as urlopen, \
         mock.patch("time.sleep"):
        with pytest.raises(urllib.error.HTTPError):
            sup._monarch_request(COOKIE, b"{}")
    assert urlopen.call_count == 3
    assert ma.AUTH_MARKER not in capsys.readouterr().out


# ── format_email: success email ───────────────────────────────────────────────

LOG = "PF Summary: Indian PF $1.00 50.00% | US PF $1.00 50.00% | Cash $0.00 0.00% | Total $2.00\n"


def test_success_email_fresh_cookie_footer_only():
    data = fe.apply_cookie_status(fe.parse(LOG), _days_ago(3), TODAY)
    html = fe.build_html(data)
    assert fe.build_subject(data).startswith("✅")
    assert f"{ma.STATUS_PREFIX} ok — 3 days old" in html
    assert "#fffbeb" not in html  # no warning banner


def test_success_email_expiring_cookie_banner_and_subject():
    data = fe.apply_cookie_status(fe.parse(LOG), _days_ago(52), TODAY)
    html = fe.build_html(data)
    assert fe.build_subject(data).startswith("⚠️")
    assert "Monarch cookie is 52 days old" in html
    assert "&lt;cookie&gt;" in html and "<cookie>" not in html  # escaped, not swallowed as a tag


def test_success_email_cookie_warning_coexists_with_sync_warnings():
    log = LOG + "WARNING: could not match Monarch accounts: ['9999']\n"
    data = fe.apply_cookie_status(fe.parse(log), _days_ago(61), TODAY)
    assert len(data["warnings"]) == 2
    html = fe.build_html(data)
    assert "could not match" in html and "61 days old" in html


def test_success_email_unset_var_unchanged():
    data = fe.apply_cookie_status(fe.parse(LOG), None, TODAY)
    html = fe.build_html(data)
    assert ma.STATUS_PREFIX not in html
    assert fe.build_subject(data).startswith("✅")


def test_success_email_no_run_url_still_shows_status():
    data = fe.apply_cookie_status(fe.parse(LOG), _days_ago(3), TODAY)
    assert data["run_url"] is None
    assert ma.STATUS_PREFIX in fe.build_html(data)


# ── format_email: failure subject ─────────────────────────────────────────────

def test_failure_subject_names_cookie_expiry():
    log = f"some output\n{ma.AUTH_MARKER} (HTTP 401). Refresh: ...\nTraceback ..."
    assert fe.build_failure_subject(log, "42") == "❌ Portfolio sync FAILED — Monarch cookie expired — 42"


@pytest.mark.parametrize("log", ["", "Traceback\nHTTP Error 502: Bad Gateway", "Kite login failed"])
def test_failure_subject_generic_otherwise(log):
    assert fe.build_failure_subject(log, "42") == "❌ Portfolio sync FAILED — 42"


# ── format_email.py as the workflow runs it ───────────────────────────────────

def _run_formatter(tmp_path, args, env_extra, log=None):
    if log is not None:
        (tmp_path / "sync_output.txt").write_text(log)
    gh_out = tmp_path / "gh_output"
    env = {**os.environ, "GITHUB_OUTPUT": str(gh_out), "PYTHONPATH": str(REPO_ROOT), **env_extra}
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "format_email.py"), *args],
                          cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return gh_out.read_text()


def test_cli_failure_with_auth_marker(tmp_path):
    out = _run_formatter(tmp_path, ["--failure"], {"GITHUB_RUN_ID": "7"}, log=f"{ma.AUTH_MARKER} (HTTP 401)\n")
    assert out == "subject=❌ Portfolio sync FAILED — Monarch cookie expired — 7\n"


def test_cli_failure_missing_log_falls_back(tmp_path):
    # Job died before the log was initialised (e.g. checkout failed after code was present).
    out = _run_formatter(tmp_path, ["--failure"], {"GITHUB_RUN_ID": "7"})
    assert out == "subject=❌ Portfolio sync FAILED — 7\n"


def test_cli_success_reads_cookie_var(tmp_path):
    out = _run_formatter(tmp_path, [], {"MONARCH_COOKIE_SET_AT": "2000-01-01"}, log=LOG)
    assert out.startswith("subject=⚠️")
    assert "overdue" in (tmp_path / "email_body.html").read_text()


def test_cli_success_without_cookie_var(tmp_path):
    env = {"MONARCH_COOKIE_SET_AT": ""}
    out = _run_formatter(tmp_path, [], env, log=LOG)
    assert out.startswith("subject=✅")


# ── update_monarch_cookie.py ──────────────────────────────────────────────────

def _run_update(monkeypatch, run_side_effect):
    monkeypatch.setattr(sys, "argv", ["update_monarch_cookie.py", COOKIE])
    monkeypatch.setattr(umc, "_get_accounts", lambda c: [{"id": "1"}])
    run = mock.MagicMock(side_effect=run_side_effect)
    monkeypatch.setattr(umc.subprocess, "run", run)
    return run


def test_update_sets_secret_then_date(monkeypatch, capsys):
    run = _run_update(monkeypatch, lambda *a, **k: subprocess.CompletedProcess(a, 0))
    umc.main()
    secret_call, var_call = (c.args[0] for c in run.call_args_list)
    assert secret_call[:3] == ["gh", "secret", "set"] and "MONARCH_COOKIE" in secret_call
    assert var_call[:4] == ["gh", "variable", "set", "MONARCH_COOKIE_SET_AT"]
    assert var_call[-1] == date.today().isoformat()
    assert "WARNING" not in capsys.readouterr().err


def test_update_variable_failure_warns_but_succeeds(monkeypatch, capsys):
    results = iter([0, 1])
    run = _run_update(monkeypatch, lambda *a, **k: subprocess.CompletedProcess(a, next(results)))
    umc.main()  # no SystemExit — secret is what matters
    assert run.call_count == 2
    assert "could not set MONARCH_COOKIE_SET_AT" in capsys.readouterr().err


def test_update_secret_failure_skips_date(monkeypatch):
    run = _run_update(monkeypatch, subprocess.CalledProcessError(1, "gh"))
    with pytest.raises(subprocess.CalledProcessError):
        umc.main()
    assert run.call_count == 1  # never recorded a date for a cookie that wasn't saved


def test_update_bad_cookie_touches_nothing(monkeypatch):
    run = _run_update(monkeypatch, None)
    monkeypatch.setattr(sys, "argv", ["update_monarch_cookie.py", "session_id=abc"])  # no csrftoken
    with pytest.raises(ValueError):
        umc.main()
    run.assert_not_called()
