"""Monarch cookie lifecycle helpers shared by the sync scripts and the email formatter.

The Monarch session cookie (MONARCH_COOKIE secret) has to be refreshed by hand from a
browser — programmatic login is CAPTCHA-blocked. Observed lifetime when harvested from a
dedicated, never-reused Chrome profile: ~60 days. These helpers make an expiry obvious
(failure email) and give advance notice (success email) instead of a raw stack trace.

Every status line starts with the stable prefix ``MONARCH_COOKIE_STATUS:`` so a human or
an agent reading the daily email can grep for it.
"""

from datetime import date

AUTH_MARKER = "[Auth] Monarch cookie expired"
STATUS_PREFIX = "MONARCH_COOKIE_STATUS:"
WARN_AFTER_DAYS = 50
TYPICAL_LIFETIME_DAYS = 60
REFRESH_HINT = (
    "Refresh: log into app.monarch.com in the Automation Chrome profile, copy the Cookie "
    "header from a graphql request, then run "
    '`update_monarch_cookie.py "<cookie>"` (portfolio-sync) and '
    "`login_setup.py` (monarch-mcp-server)."
)
_AUTH_HTTP_CODES = (401, 403)


def report_auth_failure(err: Exception) -> bool:
    """Print the auth-failure marker if ``err`` is a Monarch 401/403. Returns True if printed.

    The caller still re-raises — the step must fail. The marker line is what the failure
    email subject keys off, and it sits right above the traceback in the log.
    """
    code = getattr(err, "code", None)
    if code not in _AUTH_HTTP_CODES:
        return False
    print(f"{AUTH_MARKER} (HTTP {code}). {REFRESH_HINT}", flush=True)
    print(f"{STATUS_PREFIX} expired (HTTP {code})", flush=True)
    return True


def _parse_set_at(set_at: str | None) -> date | None:
    """Parse MONARCH_COOKIE_SET_AT ("YYYY-MM-DD", or an ISO datetime). Raises ValueError if bad."""
    value = (set_at or "").strip()
    if not value:
        return None
    return date.fromisoformat(value[:10])


def cookie_status(set_at: str | None, today: date) -> tuple[str | None, str | None]:
    """Return ``(status_line, warning)`` for the success email.

    - status_line: always-present one-liner (None only if MONARCH_COOKIE_SET_AT is unset).
    - warning: banner text when action is needed (age >= WARN_AFTER_DAYS, or bad config).
    """
    try:
        set_on = _parse_set_at(set_at)
    except ValueError:
        msg = f"MONARCH_COOKIE_SET_AT is not a valid date ({set_at!r}) — cookie age unknown."
        return f"{STATUS_PREFIX} unknown (bad MONARCH_COOKIE_SET_AT)", msg
    if set_on is None:
        return None, None

    age = (today - set_on).days
    if age < 0:
        msg = f"MONARCH_COOKIE_SET_AT ({set_on}) is in the future — cookie age unknown."
        return f"{STATUS_PREFIX} unknown (set date in future)", msg

    days = "day" if age == 1 else "days"
    if age >= TYPICAL_LIFETIME_DAYS:
        state = "overdue"
        warning = (f"Monarch cookie is {age} {days} old — past its usual ~{TYPICAL_LIFETIME_DAYS}-day "
                   f"lifetime and may expire any day. {REFRESH_HINT}")
    elif age >= WARN_AFTER_DAYS:
        state = "expiring"
        warning = (f"Monarch cookie is {age} {days} old (usually lasts ~{TYPICAL_LIFETIME_DAYS}) — "
                   f"refresh soon. {REFRESH_HINT}")
    else:
        state = "ok"
        warning = None

    status = (f"{STATUS_PREFIX} {state} — {age} {days} old (set {set_on}, "
              f"refresh due ~day {TYPICAL_LIFETIME_DAYS})")
    return status, warning
