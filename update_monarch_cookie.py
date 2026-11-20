#!/usr/bin/env python3
"""Validate a Monarch Money browser cookie and push it to the MONARCH_COOKIE GitHub secret.

Usage:
    python update_monarch_cookie.py
    python update_monarch_cookie.py "session_id=...; csrftoken=...; ..."

See README.md "Monarch Money cookie" for how to pull the cookie from DevTools.
"""

import getpass
import json
import subprocess
from datetime import date
import sys
import urllib.request

REPO = "harshitbshah/portfolio-sync"
REQUIRED_COOKIES = ("session_id", "csrftoken")


def _cookie_value(cookie: str, name: str) -> str:
    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    raise ValueError(f"Cookie '{name}' not found — did you copy the full Cookie header?")


def _get_accounts(cookie: str) -> list:
    req = urllib.request.Request(
        "https://api.monarch.com/graphql",
        data=json.dumps({"query": "{ accounts { id displayName } }"}).encode(),
        headers={
            "Cookie": cookie,
            "X-Csrftoken": _cookie_value(cookie, "csrftoken"),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Client-Platform": "web",
            "Origin": "https://app.monarch.com",
            "Referer": "https://app.monarch.com/",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result.get("data", {}).get("accounts", [])


def main() -> None:
    cookie = sys.argv[1] if len(sys.argv) > 1 else getpass.getpass(
        "Paste the Cookie header value from a monarch.com/graphql request: "
    )
    cookie = cookie.strip()

    print("Checking cookie has required keys...")
    for name in REQUIRED_COOKIES:
        _cookie_value(cookie, name)  # raises ValueError if missing

    print("Verifying against the Monarch API...")
    accounts = _get_accounts(cookie)
    if not accounts:
        print("ERROR: cookie looked valid but the API returned no accounts.", file=sys.stderr)
        sys.exit(1)
    print(f"  OK — found {len(accounts)} accounts.")

    print(f"Updating MONARCH_COOKIE secret on {REPO}...")
    subprocess.run(
        ["gh", "secret", "set", "MONARCH_COOKIE", "--repo", REPO, "--body", cookie],
        check=True,
    )
    set_at = date.today().isoformat()
    print(f"Recording refresh date MONARCH_COOKIE_SET_AT={set_at} (drives the expiry reminder)...")
    result = subprocess.run(
        ["gh", "variable", "set", "MONARCH_COOKIE_SET_AT", "--repo", REPO, "--body", set_at],
        check=False,
    )
    if result.returncode != 0:
        # The secret is already updated — the sync will work. Only the reminder date is stale.
        print("WARNING: could not set MONARCH_COOKIE_SET_AT; the expiry reminder will be off. "
              f"Set it manually: gh variable set MONARCH_COOKIE_SET_AT --repo {REPO} --body {set_at}",
              file=sys.stderr)
    print("Done. Trigger a manual workflow run to confirm the fix.")


if __name__ == "__main__":
    main()
