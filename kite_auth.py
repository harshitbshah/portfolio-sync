#!/usr/bin/env python3
"""Generate a Kite Connect access token via automated login (requests + pyotp).

Writes the access_token to GITHUB_OUTPUT for downstream workflow steps.

Login flow:
  1. GET kite.trade/connect/login  → follows redirects to zerodha login page;
                                     captures final URL which contains sess_id
  2. POST /api/login               → Zerodha credentials → get request_id
  3. POST /api/twofa               → TOTP; sets enctoken/user_id cookies
  4. GET <step-1-url>&skip_session=true  → server skips consent page;
                                     follows redirects to redirect_url?request_token=…
  5. POST api.kite.trade/session/token  → exchange request_token for access_token
"""

import hashlib
import os

import pyotp
import requests
from urllib.parse import urlparse, parse_qs


def login() -> str:
    api_key    = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    user_id    = os.environ["ZERODHA_USER_ID"]
    password   = os.environ["ZERODHA_PASSWORD"]
    totp_key   = os.environ["ZERODHA_TOTP_KEY"]

    connect_url = f"https://kite.trade/connect/login?v=3&api_key={api_key}"
    s = requests.Session()

    # Step 1: init Kite Connect OAuth session; capture the zerodha login URL with sess_id
    init_r = s.get(connect_url, timeout=15)
    login_url = init_r.url  # https://kite.zerodha.com/connect/login?api_key=...&sess_id=...

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

    # Step 3: submit TOTP — sets enctoken + user_id cookies on the session
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

    # Step 4: re-hit the original login URL with skip_session=true.
    # This tells Zerodha to bypass the consent UI and immediately redirect to
    # the app's redirect_url with request_token.
    skip_url = login_url + ("&" if "?" in login_url else "?") + "skip_session=true"
    request_token = None
    # Try several approaches to bypass/complete the consent step
    base = "https://kite.zerodha.com"
    sess_id = parse_qs(urlparse(login_url).query).get("sess_id", [None])[0] or ""
    attempts = [
        skip_url,  # original URL + skip_session=true
        f"{base}/connect/authorize?api_key={api_key}&sess_id={sess_id}&skip_session=true",
        f"{base}/connect/finish?api_key={api_key}&sess_id={sess_id}",
        f"{base}/connect/authorize?api_key={api_key}&sess_id={sess_id}&action=allow",
        f"{base}/connect/authorize?api_key={api_key}&sess_id={sess_id}&skip_session=1",
    ]

    for attempt_url in attempts:
        try:
            resp = s.get(attempt_url, allow_redirects=True, timeout=15)
            rt = parse_qs(urlparse(resp.url).query).get("request_token", [None])[0]
            print(f"  GET {attempt_url.replace(api_key, '***').replace(sess_id, '***')} → {resp.status_code} url={resp.url.replace(api_key, '***').replace(sess_id, '***')!r} rt={rt!r}")
            if rt:
                request_token = rt
                break
        except requests.exceptions.ConnectionError as e:
            err_url = str(e.request.url) if (hasattr(e, "request") and e.request) else ""
            rt = parse_qs(urlparse(err_url).query).get("request_token", [None])[0]
            print(f"  GET {attempt_url.replace(api_key, '***')} → ConnErr rt={rt!r}")
            if rt:
                request_token = rt
                break

    if not request_token:
        raise RuntimeError("Could not extract request_token from redirect chain.")

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
