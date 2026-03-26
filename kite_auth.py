#!/usr/bin/env python3
"""Generate a Kite Connect access token via automated login (requests + pyotp).

Writes the access_token to GITHUB_OUTPUT for downstream workflow steps.

Login flow:
  1. GET kite.trade/connect/login  → initialises Kite Connect OAuth session
  2. POST /api/login               → Zerodha credentials
  3. POST /api/twofa               → TOTP (returns 200 + profile, no redirect yet)
  4. GET kite.trade/connect/login  → re-hit with authenticated cookies;
                                     Kite redirects to /connect/authorize?sess_id=…
  5. POST /connect/authorize       → confirm app authorization (sess_id in body);
                                     Kite redirects to redirect_url?request_token=…
  6. POST /session/token           → exchange request_token for access_token
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
    # The final URL contains the sess_id we must reuse in step 4
    init_r = s.get(connect_url, timeout=15)
    init_params = parse_qs(urlparse(init_r.url).query)
    original_sess_id = init_params.get("sess_id", [None])[0]
    print(f"  step1 final URL: {init_r.url!r}")

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
    # IMPORTANT: reuse the original sess_id URL from step 1 — hitting connect_url creates a new session.
    request_token = _extract_request_token(r, s, api_key)

    if not request_token:
        print("  twofa gave no redirect — re-triggering Kite Connect OAuth...")
        retrigger_url = init_r.url if original_sess_id else connect_url
        print(f"  re-trigger URL: {retrigger_url!r}")
        try:
            r = s.get(retrigger_url, allow_redirects=True, timeout=15)
            authorize_url = r.url
            print(f"  connect re-hit final URL: {authorize_url!r}")
            final_params = parse_qs(urlparse(authorize_url).query)
            request_token = final_params.get("request_token", [None])[0]

            if not request_token and "sess_id" in final_params:
                # Landed on the OAuth consent page — POST to /api/connect/app/authorize
                sess_id = final_params["sess_id"][0]
                print(f"  authorize consent page — calling /api/connect/app/authorize")
                # /api/connect/session accepts GET — get session info to find app_id
                base = "https://kite.zerodha.com"
                sess_info_r = s.get(f"{base}/api/connect/session",
                                    params={"api_key": api_key, "sess_id": sess_id}, timeout=15)
                sess_data = sess_info_r.json().get("data", {})
                app_id = sess_data.get("app_id", "")
                print(f"  connect session: app_id={app_id!r} redirect_params={sess_data.get('redirect_params')!r}")
                candidates = [
                    ("POST", f"{base}/api/connect/app/authorize", {"sess_id": sess_id, "api_key": api_key}, None),
                    ("POST", f"{base}/api/apps/{app_id}/authorize",  {"sess_id": sess_id}, None),
                    ("GET",  f"{base}/api/connect/app/authorize", None, {"api_key": api_key, "sess_id": sess_id}),
                    ("PUT",  f"{base}/api/connect/session",  {"status": "authorized"}, {"api_key": api_key, "sess_id": sess_id}),
                ]
                for method, url, body, params in candidates:
                    try:
                        resp = s.request(method, url, data=body, params=params,
                                         allow_redirects=True, timeout=15)
                        rt = parse_qs(urlparse(resp.url).query).get("request_token", [None])[0]
                        short = url.replace("https://kite.zerodha.com", "")
                        print(f"  {method} {short} → {resp.status_code} url={resp.url!r} rt={rt!r} body={resp.text[:200]!r}")
                        if rt:
                            r = resp
                            request_token = rt
                            break
                    except requests.exceptions.ConnectionError as ce:
                        err_url = str(ce.request.url) if (hasattr(ce, "request") and ce.request) else ""
                        rt = parse_qs(urlparse(err_url).query).get("request_token", [None])[0]
                        print(f"  {method} {url} → ConnErr url={err_url!r} rt={rt!r}")
                        if rt:
                            request_token = rt
                            break
        except requests.exceptions.ConnectionError as e:
            # Redirect chain ended at 127.0.0.1 — extract from the failed request URL
            url = str(e.request.url) if (hasattr(e, "request") and e.request) else ""
            print(f"  ConnectionError URL: {url!r}")
            request_token = parse_qs(urlparse(url).query).get("request_token", [None])[0]

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
