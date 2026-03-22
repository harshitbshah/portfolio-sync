#!/usr/bin/env python3
"""Sync Zerodha (Indian PF) balance from Google Sheets to Monarch Money."""

import json
import os
import re
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config (all overridable via environment variables) ────────────────────────
SHEET_ID = os.environ["GSHEET_SHEET_ID"]
SHEET_TAB = os.getenv("GSHEET_TAB", "PF Summary")
LABEL_TO_FIND = os.getenv("GSHEET_LABEL", "Indian PF")
MONARCH_ACCOUNT_NAME = os.getenv("MONARCH_ACCOUNT_NAME", "Zerodha")


# ── Step 1: Read balance from Google Sheets ───────────────────────────────────
def get_indian_pf_balance() -> float:
    raw_key = os.environ["GSHEET_SERVICE_ACCOUNT_JSON"]
    key_info = json.loads(raw_key)

    creds = service_account.Credentials.from_service_account_info(
        key_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{SHEET_TAB}'")
        .execute()
    )
    rows = result.get("values", [])
    for row in rows:
        for i, cell in enumerate(row):
            if cell.strip() == LABEL_TO_FIND and i + 1 < len(row):
                raw = row[i + 1].strip()
                clean = re.sub(r"[^\d.]", "", raw)
                return float(clean)
    raise ValueError(f"Could not find row labeled '{LABEL_TO_FIND}' in sheet")


# ── Step 2: Update Monarch Money ──────────────────────────────────────────────
def monarch_request(token: str, payload: bytes) -> dict:
    import urllib.request
    req = urllib.request.Request(
        "https://api.monarch.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Client-Platform": "web",
            "User-Agent": "MonarchMoneyAPI (https://github.com/bradleyseanf/monarchmoneycommunity)",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_monarch_account_id(token: str) -> str:
    payload = json.dumps({
        "query": "{ accounts { id displayName } }",
    }).encode()
    result = monarch_request(token, payload)
    accounts = result.get("data", {}).get("accounts", [])
    for account in accounts:
        if account.get("displayName") == MONARCH_ACCOUNT_NAME:
            return account["id"]
    names = [a.get("displayName") for a in accounts]
    raise ValueError(f"No Monarch account named '{MONARCH_ACCOUNT_NAME}'. Found: {names}")


def update_monarch(balance: float) -> None:
    token = os.environ["MONARCH_TOKEN"]

    print(f"  Looking up Monarch account '{MONARCH_ACCOUNT_NAME}'...")
    account_id = get_monarch_account_id(token)
    print(f"  Found account ID: {account_id}")

    query = """
    mutation Common_UpdateAccount($input: UpdateAccountMutationInput!) {
        updateAccount(input: $input) {
            account {
                id
                displayName
                displayBalance
            }
            errors {
                message
            }
        }
    }
    """
    payload = json.dumps({
        "query": query,
        "variables": {
            "input": {
                "id": account_id,
                "displayBalance": balance,
            }
        },
    }).encode()

    result = monarch_request(token, payload)

    errors = result.get("data", {}).get("updateAccount", {}).get("errors", [])
    if errors:
        print(f"ERROR: {errors}", file=sys.stderr)
        sys.exit(1)

    updated = result.get("data", {}).get("updateAccount", {}).get("account", {})
    print(f"Updated: {updated.get('displayName')} → ${updated.get('displayBalance'):,.2f}")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Fetching Indian PF balance from Google Sheets...")
    balance = get_indian_pf_balance()
    print(f"  Found: ${balance:,.2f}")

    print("Updating Monarch Money...")
    update_monarch(balance)
    print("Done.")
