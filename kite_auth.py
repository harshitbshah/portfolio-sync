#!/usr/bin/env python3
"""Generate a Kite Connect access token via automated login (requests + pyotp).

Writes the access_token to GITHUB_OUTPUT for downstream workflow steps.
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

    s = requests.Session()

    # Step 1: initialise login session (sets cookies)
    s.get(
        f"https://kite.trade/connect/login?v=3&api_key={api_key}",
        timeout=15,
    )

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

    # Step 3: submit TOTP — follow redirects manually to capture request_token
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

    print(f"  twofa status: {r.status_code}")
    print(f"  twofa headers: {dict(r.headers)}")
    try:
        print(f"  twofa body: {r.text[:500]}")
    except Exception:
        pass

    request_token = None

    # Check twofa JSON body first (some flows return token in body)
    try:
        body = r.json()
        rt = body.get("data", {}).get("request_token")
        if rt:
            request_token = rt
            print(f"  request_token found in twofa JSON body")
    except Exception:
        pass

    if not request_token:
        for _ in range(10):
            location = r.headers.get("Location", "")
            print(f"  redirect location: {location!r}")
            params = parse_qs(urlparse(location).query)
            if "request_token" in params:
                request_token = params["request_token"][0]
                break
            if not location or r.status_code not in (301, 302, 303, 307, 308):
                break
            try:
                r = s.get(location, allow_redirects=False, timeout=10)
                print(f"  followed redirect → status: {r.status_code}")
            except requests.exceptions.ConnectionError:
                # Redirect to 127.0.0.1 — parse request_token from the Location header
                params = parse_qs(urlparse(location).query)
                if "request_token" in params:
                    request_token = params["request_token"][0]
                break

    if not request_token:
        raise RuntimeError("Could not extract request_token from login redirect")

    # Step 4: exchange request_token for access_token
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
