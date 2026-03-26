#!/usr/bin/env python3
"""Generate a Kite Connect access token via automated login (requests + pyotp).

Writes the access_token to GITHUB_OUTPUT for downstream workflow steps.

Login flow:
  1. GET kite.trade/connect/login  → initialises Kite Connect OAuth session
  2. POST /api/login               → Zerodha credentials
  3. POST /api/twofa               → TOTP (returns 200 + profile, no redirect yet)
  4. GET kite.trade/connect/login  → re-hit with authenticated cookies;
                                     Kite now redirects to redirect_url?request_token=…
  5. POST /session/token           → exchange request_token for access_token
"""

import hashlib
import os

import pyotp
import requests
from urllib.parse import urlparse, parse_qs


def _extract_request_token(r, s, api_key) -> str | None:
    """Follow redirect chain until request_token is found. Returns token or None."""
    for _ in range(10):
        location = r.headers.get("Location", "")
        params = parse_qs(urlparse(location).query)
        if "request_token" in params:
            return params["request_token"][0]
        if not location or r.status_code not in (301, 302, 303, 307, 308):
            return None
        try:
            r = s.get(location, allow_redirects=False, timeout=10)
        except requests.exceptions.ConnectionError:
            params = parse_qs(urlparse(location).query)
            return params["request_token"][0] if "request_token" in params else None
    return None


def login() -> str:
    api_key    = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    user_id    = os.environ["ZERODHA_USER_ID"]
    password   = os.environ["ZERODHA_PASSWORD"]
    totp_key   = os.environ["ZERODHA_TOTP_KEY"]

    connect_url = f"https://kite.trade/connect/login?v=3&api_key={api_key}"
    s = requests.Session()

    # Step 1: initialise Kite Connect OAuth session (sets app-context cookies)
    s.get(connect_url, timeout=15)

    # Step 2: submit credentials
    r = s.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": user_id, "password": password},
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Login failed: {payload.get('message')}")
    request_id = payload["data"]["request_id"]

    # Step 3: submit TOTP — completes web login (returns 200 + profile, no redirect yet)
    r = s.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":     user_id,
            "request_id":  request_id,
            "twofa_value": pyotp.TOTP(totp_key).now(),
            "twofa_type":  "totp",
        },
        allow_redirects=False,
        timeout=15,
    )
    r.raise_for_status()
    print(f"  twofa status: {r.status_code}")

    # Step 4: re-hit the Kite Connect login URL with now-authenticated session.
    # Kite detects the active session and redirects to redirect_url?request_token=…
    request_token = _extract_request_token(r, s, api_key)

    if not request_token:
        print("  twofa gave no redirect — re-triggering Kite Connect OAuth...")
        r = s.get(connect_url, allow_redirects=False, timeout=15)
        print(f"  connect re-hit status: {r.status_code}, location: {r.headers.get('Location', '')!r}")
        request_token = _extract_request_token(r, s, api_key)

    if not request_token:
        raise RuntimeError(
            f"Could not extract request_token. "
            f"Last status: {r.status_code}, Location: {r.headers.get('Location', '')!r}"
        )

    # Step 5: exchange request_token for access_token
    checksum = hashlib.sha256(
        f"{api_key}{request_token}{api_secret}".encode()
    ).hexdigest()

    r = s.post(
        "https://api.kite.trade/session/token",
        data={
            "api_key":       api_key,
            "request_token": request_token,
            "checksum":      checksum,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Session generation failed: {data.get('message')}")

    return data["data"]["access_token"]


if __name__ == "__main__":
    token = login()
    print("Kite access token generated successfully.")

    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"access_token={token}\n")
    else:
        print(f"KITE_ACCESS_TOKEN={token}")
