#!/usr/bin/env python3
"""Sync between Zerodha/Monarch Money and Google Sheets (bidirectional)."""

import json
import os
import re
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID = os.environ["GSHEET_SHEET_ID"]
SHEET_TAB = os.getenv("GSHEET_TAB", "PF Summary")
LABEL_TO_FIND = os.getenv("GSHEET_LABEL", "Indian PF")
MONARCH_ACCOUNT_NAME = os.getenv("MONARCH_ACCOUNT_NAME", "Zerodha")

# Emergency fund account names in Monarch → sheet row number (1-indexed, row 1 = header)
EMERGENCY_FUND_ACCOUNTS = json.loads(os.getenv("EMERGENCY_FUND_ACCOUNTS_JSON", json.dumps([
    {"name": "Checking (...8843)",           "row": 2},
    {"name": "TOTAL CHECKING (...6986)",     "row": 3},
    {"name": "ICICI",                        "row": 4},
    {"name": "PayPal",                       "row": 5},
    {"name": "PPF",                          "row": 6},
    {"name": "14 MONTH CD (...9868)",        "row": 7},
    {"name": "Certificate of Deposit (...3294)", "row": 8},
    {"name": "Certificate of Deposit (...6677)", "row": 9},
])))

SGOV_TOTAL_CELL = os.getenv("SGOV_TOTAL_CELL", "F5")


# ── Google Sheets helpers ─────────────────────────────────────────────────────
def _sheets_service(readonly: bool = True):
    raw_key = os.environ["GSHEET_SERVICE_ACCOUNT_JSON"]
    key_info = json.loads(raw_key)
    scope = (
        "https://www.googleapis.com/auth/spreadsheets.readonly"
        if readonly
        else "https://www.googleapis.com/auth/spreadsheets"
    )
    creds = service_account.Credentials.from_service_account_info(
        key_info, scopes=[scope]
    )
    return build("sheets", "v4", credentials=creds)


# ── Step 1: Read Indian PF balance from Google Sheets ─────────────────────────
def get_indian_pf_balance() -> float:
    service = _sheets_service(readonly=True)
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


# ── Step 2: Update Monarch Money (Zerodha balance) ───────────────────────────
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


def get_monarch_accounts(token: str) -> list:
    payload = json.dumps({
        "query": "{ accounts { id displayName isHidden deactivatedAt displayBalance type { name } } }",
    }).encode()
    result = monarch_request(token, payload)
    return result.get("data", {}).get("accounts", [])


def get_monarch_account_id(token: str) -> str:
    accounts = get_monarch_accounts(token)
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


# ── Step 3: Read emergency fund balances + SGOV total from Monarch ────────────
def get_emergency_fund_balances(token: str) -> dict[str, float]:
    """Return {account_name: balance} for all emergency fund accounts."""
    accounts = get_monarch_accounts(token)
    target_names = {entry["name"] for entry in EMERGENCY_FUND_ACCOUNTS}
    balances = {}
    for account in accounts:
        name = account.get("displayName", "")
        if name in target_names:
            balances[name] = account.get("displayBalance", 0.0) or 0.0
    missing = target_names - set(balances)
    if missing:
        print(f"  WARNING: could not find Monarch accounts: {missing}", file=sys.stderr)
    return balances


def get_sgov_total(token: str) -> float:
    """Sum SGOV quantity across all active brokerage accounts."""
    accounts = get_monarch_accounts(token)
    brokerage_ids = [
        a["id"] for a in accounts
        if a.get("type", {}).get("name") == "brokerage" and not a.get("deactivatedAt")
    ]

    query = """
    query GetHoldings($accountId: ID!) {
        portfolio(input: { accountIds: [$accountId] }) {
            aggregateHoldings {
                edges {
                    node {
                        quantity
                        holdings {
                            ticker
                        }
                    }
                }
            }
        }
    }
    """

    total_sgov = 0.0
    for account_id in brokerage_ids:
        payload = json.dumps({
            "query": query,
            "variables": {"accountId": account_id},
        }).encode()
        result = monarch_request(token, payload)
        edges = (
            result.get("data", {})
            .get("portfolio", {})
            .get("aggregateHoldings", {})
            .get("edges", [])
        )
        for edge in edges:
            node = edge.get("node", {})
            holdings = node.get("holdings", [])
            for holding in holdings:
                if holding.get("ticker") == "SGOV":
                    total_sgov += node.get("quantity", 0.0)
                    break

    return round(total_sgov, 6)


# ── Step 4: Write balances back to Google Sheets ──────────────────────────────
def update_google_sheet(balances: dict[str, float], sgov_total: float) -> None:
    service = _sheets_service(readonly=False)
    spreadsheets = service.spreadsheets()

    # Build batch update: emergency fund balances
    data = []
    for entry in EMERGENCY_FUND_ACCOUNTS:
        name = entry["name"]
        row = entry["row"]
        balance = balances.get(name)
        if balance is None:
            print(f"  Skipping '{name}' (not found in Monarch)")
            continue
        cell = f"'{SHEET_TAB}'!C{row}"
        data.append({"range": cell, "values": [[round(balance, 2)]]})
        print(f"  {name} → C{row}: ${balance:,.2f}")

    # SGOV total
    sgov_cell = f"'{SHEET_TAB}'!{SGOV_TOTAL_CELL}"
    data.append({"range": sgov_cell, "values": [[sgov_total]]})
    print(f"  SGOV total → {SGOV_TOTAL_CELL}: {sgov_total:,.4f} shares")

    spreadsheets.values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token = os.environ["MONARCH_TOKEN"]

    # Zerodha → Monarch
    print("Fetching Indian PF balance from Google Sheets...")
    balance = get_indian_pf_balance()
    print(f"  Found: ${balance:,.2f}")
    print("Updating Monarch Money (Zerodha)...")
    update_monarch(balance)

    # Monarch → Google Sheets
    print("\nFetching emergency fund balances from Monarch...")
    balances = get_emergency_fund_balances(token)
    print(f"  Found {len(balances)} accounts")

    print("Fetching SGOV total from Monarch...")
    sgov_total = get_sgov_total(token)
    print(f"  SGOV total: {sgov_total:,.4f} shares")

    print("Writing to Google Sheets...")
    update_google_sheet(balances, sgov_total)

    print("\nDone.")
