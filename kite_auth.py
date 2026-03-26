#!/usr/bin/env python3
"""Generate a Kite Connect access token via automated login (requests + pyotp).

Writes the access_token to GITHUB_OUTPUT for downstream workflow steps.

Login flow:
  1. GET kite.trade/connect/login  → follows redirects; captures final URL (has sess_id)
  2. POST /api/login               → Zerodha credentials → get request_id
  3. POST /api/twofa               → TOTP + skip_session=True; tells server to skip
                                     the OAuth consent page for this session
  4. GET kite.trade/connect/login  → with authenticated cookies + skip_session set,
                                     server redirects to redirect_url?request_token=…
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

    # Step 3: submit TOTP — skip_session tells Zerodha to skip the OAuth consent page
    # Try with allow_redirects=True: if twofa redirects directly to redirect_url, capture it
    r = s.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":        user_id,
            "request_id":     request_id,
            "twofa_value":    pyotp.TOTP(totp_key).now(),
            "twofa_type":     "totp",
            "skip_session":   "true",
        },
        allow_redirects=True,
        timeout=15,
    )
    r.raise_for_status()
    print(f"  twofa status={r.status_code} final_url={r.url!r}")

    # Step 4: chain is /connect/login → /connect/finish → redirect_url?request_token=…
    # Follow each 302 manually so we can see exactly where each hop lands.
    request_token = None

    def _extract_token(url):
        return parse_qs(urlparse(url).query).get("request_token", [None])[0]

    def _try_finish_post(finish_url):
        """POST to /connect/finish — some OAuth flows confirm via POST, not GET."""
        try:
            r = s.post(finish_url, allow_redirects=False, timeout=15)
            loc = r.headers.get("Location", "")
            print(f"  POST finish → {r.status_code} Location={loc!r}")
            return _extract_token(loc) or _extract_token(r.url)
        except requests.exceptions.ConnectionError as e:
            err_url = str(e.request.url) if (hasattr(e, "request") and e.request) else ""
            return _extract_token(err_url)

    def _follow_chain(start_url):
        """Follow up to 3 GET hops; when we hit /connect/finish also try POSTing it."""
        url = start_url
        for hop in range(3):
            try:
                r = s.get(url, allow_redirects=False, timeout=15)
                loc = r.headers.get("Location", "")
                print(f"  hop{hop} GET → {r.status_code} url={r.url!r} Location={loc!r}")
                for candidate in [loc, r.url]:
                    token = _extract_token(candidate)
                    if token:
                        return token
                if r.status_code == 302 and loc:
                    # If next hop is the consent page, try POSTing to finish instead
                    if "/connect/authorize" in loc and "/connect/finish" in url:
                        token = _try_finish_post(url)
                        if token:
                            return token
                    url = loc
                else:
                    return None
            except requests.exceptions.ConnectionError as e:
                err_url = str(e.request.url) if (hasattr(e, "request") and e.request) else ""
                print(f"  hop{hop} ConnErr url={err_url!r}")
                return _extract_token(err_url)
        return None

    request_token = _follow_chain(login_url) or _follow_chain(connect_url)

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
