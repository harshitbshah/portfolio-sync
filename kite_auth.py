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
    print(f"  step1 cookies: {[(c.name, c.domain) for c in s.cookies]}")

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
    print(f"  login cookies after: {[(c.name, c.domain) for c in s.cookies]}")

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
    print(f"  twofa cookies: {[(c.name, c.domain) for c in s.cookies]}")

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
                # Find the connect-authorize chunk files in index.js to locate the authorize API call
                import re as _re
                idx_js = s.get("https://kite.zerodha.com/static/js/index.c752df4a.js", timeout=15)
                # Find chunk ID → filename mapping (Webpack: e.g. 526:"abc123" maps chunk 526 to abc123.js)
                chunk_map = dict(_re.findall(r'(\d+):"([a-f0-9]{8})"', idx_js.text))
                print(f"  relevant chunks: 526→{chunk_map.get('526')}, 126→{chunk_map.get('126')}")
                base = "https://kite.zerodha.com"
                # Find Webpack chunk URL pattern in index.js
                # Look for how chunks are loaded (e.g. "p+'/'+{526:'abc',126:'def'}[e]+'.js'")
                wp_patterns = []
                for pat in [r'static/js.{0,200}', r'\d+:.{1,50}\.js', r'chunkId.{0,100}js']:
                    for m in _re.finditer(pat, idx_js.text):
                        ctx = idx_js.text[max(0,m.start()-20):m.end()+20]
                        wp_patterns.append(ctx[:120])
                        break
                print(f"  webpack chunk URL patterns: {wp_patterns}")
                # Try 08797ac7 (hash for chunk 126) with several URL patterns
                for url_pat in ["08797ac7.js", "126.08797ac7.js", "08797ac7.126.js",
                                 "08797ac7.chunk.js", "chunk.08797ac7.js"]:
                    try:
                        r2 = s.get(f"{base}/static/js/{url_pat}", timeout=5)
                        print(f"  {url_pat}: {r2.status_code} size={len(r2.text)}")
                        if r2.status_code == 200 and len(r2.text) > 100:
                            for pat in ["authorize", "sess_id", "confirm"]:
                                for m in _re.finditer(pat, r2.text):
                                    print(f"  126 '{pat}': {r2.text[max(0,m.start()-50):m.end()+80]!r}")
                                    break
                    except Exception:
                        pass
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
