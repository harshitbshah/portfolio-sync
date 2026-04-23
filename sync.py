#!/usr/bin/env python3
"""Sync between Zerodha/Monarch Money and Google Sheets (bidirectional)."""

import json
import os
import re
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID = os.environ["GSHEET_SHEET_ID"]
SHEET_TAB = os.getenv("GSHEET_TAB", "PF Summary")
LABEL_TO_FIND = os.getenv("GSHEET_LABEL", "Indian PF")
MONARCH_ACCOUNT_NAME = os.getenv("MONARCH_ACCOUNT_NAME", "Zerodha")

# Maps Monarch accounts to sheet rows via stable identifiers.
# Use "mask" (last 4 digits) for institution-synced accounts.
# Use "monarch_name" for manual accounts (no mask).
# sheet_category + sheet_institution locate the row dynamically at runtime.
# For duplicate category+institution (e.g. two Chase or two Marcus rows),
# entries are matched in the order they appear in the sheet.
SHEET_ACCOUNTS = json.loads(os.environ["ACCOUNTS_JSON"])

# Label to search for in the sheet to locate the SGOV quantity cell.
# The value is written to the cell immediately to the right of this label.
SGOV_LABEL = os.getenv("SGOV_LABEL", "Total:")
PF_BREAKDOWN_LABEL = os.getenv("PF_BREAKDOWN_LABEL", "PF Breakdown")


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


def _read_sheet_rows() -> list:
    """Return all rows from the sheet tab."""
    service = _sheets_service(readonly=True)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{SHEET_TAB}'")
        .execute()
    )
    return result.get("values", [])


# ── Step 1: Read Indian PF balance from Google Sheets ─────────────────────────
def get_indian_pf_balance() -> float:
    rows = _read_sheet_rows()
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
        "query": "{ accounts { id displayName isHidden deactivatedAt displayBalance mask type { name } } }",
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


# ── Step 3: Read account balances + SGOV total from Monarch ──────────────────
def get_account_balances(token: str) -> dict[str, float]:
    """Match Monarch accounts to SHEET_ACCOUNTS entries by mask or monarch_name.

    Returns {entry_index: balance} keyed by position in SHEET_ACCOUNTS so
    duplicate category+institution entries are handled unambiguously.
    """
    accounts = get_monarch_accounts(token)
    balances: dict[int, float] = {}
    unmatched: list[int] = []

    for i, entry in enumerate(SHEET_ACCOUNTS):
        matched = None
        if "mask" in entry:
            for acct in accounts:
                if acct.get("mask") == entry["mask"]:
                    matched = acct
                    break
        elif "monarch_name" in entry:
            for acct in accounts:
                if acct.get("displayName") == entry["monarch_name"]:
                    matched = acct
                    break

        if matched is not None:
            balances[i] = matched.get("displayBalance", 0.0) or 0.0
        else:
            unmatched.append(i)

    if unmatched:
        descs = [
            entry.get("mask") or entry.get("monarch_name")
            for entry in (SHEET_ACCOUNTS[i] for i in unmatched)
        ]
        print(f"  WARNING: could not match Monarch accounts: {descs}", file=sys.stderr)

    return balances


_SGOV_HOLDINGS_QUERY = """
query GetHoldings($accountId: ID!) {
    portfolio(input: { accountIds: [$accountId] }) {
        aggregateHoldings {
            edges {
                node {
                    quantity
                    totalValue
                    holdings {
                        ticker
                    }
                }
            }
        }
    }
}
"""


def get_sgov_total(token: str) -> float:
    """Sum SGOV quantity across all active brokerage accounts."""
    accounts = get_monarch_accounts(token)
    brokerage_ids = [
        a["id"] for a in accounts
        if a.get("type", {}).get("name") == "brokerage" and not a.get("deactivatedAt")
    ]

    total_sgov = 0.0
    for account_id in brokerage_ids:
        payload = json.dumps({
            "query": _SGOV_HOLDINGS_QUERY,
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


def print_sgov_breakdown(token: str) -> float:
    """Print per-account SGOV holdings as structured log lines and return total."""
    accounts = get_monarch_accounts(token)
    brokerage_accounts = [
        a for a in accounts
        if a.get("type", {}).get("name") == "brokerage" and not a.get("deactivatedAt")
    ]

    total_sgov = 0.0
    for acct in brokerage_accounts:
        account_id = acct["id"]
        name = acct.get("displayName", account_id)
        payload = json.dumps({
            "query": _SGOV_HOLDINGS_QUERY,
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
                    qty = node.get("quantity", 0.0)
                    value = node.get("totalValue", 0.0) or 0.0
                    total_sgov += qty
                    print(f"[SGOV] {name}: ${value:.2f}")
                    break

    return round(total_sgov, 6)


# ── Step 4: Write balances back to Google Sheets ──────────────────────────────
def _resolve_sheet_rows(rows: list) -> list[int | None]:
    """Return sheet row number (1-indexed) for each entry in SHEET_ACCOUNTS.

    Matches by sheet_category (col A) + sheet_institution (col B) in order,
    consuming each match so duplicate rows are assigned correctly.
    """
    # Build ordered list of (category, institution, 1-indexed row)
    candidates = []
    for row_idx, row in enumerate(rows):
        cat = row[0].strip() if len(row) > 0 else ""
        inst = row[1].strip() if len(row) > 1 else ""
        if cat and inst:
            candidates.append([cat, inst, row_idx + 1, False])  # False = not yet used

    result: list[int | None] = []
    for entry in SHEET_ACCOUNTS:
        cat = entry["sheet_category"]
        inst = entry["sheet_institution"]
        found = None
        for candidate in candidates:
            if candidate[0] == cat and candidate[1] == inst and not candidate[3]:
                candidate[3] = True  # mark as used
                found = candidate[2]
                break
        if found is None:
            print(f"  WARNING: could not find sheet row for {cat}/{inst}", file=sys.stderr)
        result.append(found)
    return result


def _find_sgov_cell(rows: list) -> str:
    """Return the A1 address of the cell to the right of SGOV_LABEL."""
    col_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for row_idx, row in enumerate(rows):
        for col_idx, cell in enumerate(row):
            if cell.strip() == SGOV_LABEL and col_idx + 1 < 26:
                return f"{col_letters[col_idx + 1]}{row_idx + 1}"
    raise ValueError(f"Could not find SGOV label '{SGOV_LABEL}' in sheet")


def update_google_sheet(balances: dict[int, float], sgov_total: float) -> None:
    rows = _read_sheet_rows()
    row_numbers = _resolve_sheet_rows(rows)
    sgov_cell = _find_sgov_cell(rows)

    service = _sheets_service(readonly=False)
    data = []

    for i, entry in enumerate(SHEET_ACCOUNTS):
        balance = balances.get(i)
        row = row_numbers[i]
        if balance is None or row is None:
            label = entry.get("mask") or entry.get("monarch_name")
            print(f"  Skipping '{label}' (not matched)")
            continue
        cell = f"'{SHEET_TAB}'!C{row}"
        data.append({"range": cell, "values": [[round(balance, 2)]]})
        label = entry.get("mask") or entry.get("monarch_name")
        print(f"  {label} → C{row}: ${balance:,.2f}")

    data.append({"range": f"'{SHEET_TAB}'!{sgov_cell}", "values": [[sgov_total]]})
    print(f"  SGOV total → {sgov_cell}: {sgov_total:,.4f} shares")

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


# ── Step 5: Fetch Monarch net worth ───────────────────────────────────────────
def print_net_worth(token: str) -> None:
    """Fetch today's aggregate net worth from Monarch and emit a log line."""
    import datetime
    today = datetime.date.today().isoformat()
    payload = json.dumps({
        "query": """
        query GetAggregateSnapshots($filters: AggregateSnapshotFilters) {
            aggregateSnapshots(filters: $filters) {
                date
                balance
            }
        }
        """,
        "variables": {"filters": {"startDate": today, "endDate": today}},
    }).encode()
    result = monarch_request(token, payload)
    snapshots = result.get("data", {}).get("aggregateSnapshots", [])
    if snapshots:
        balance = snapshots[-1]["balance"]
        print(f"[Monarch] Net Worth: ${balance:,.2f}")
    else:
        print("WARNING: No aggregate snapshot returned from Monarch", file=sys.stderr)


# ── Step 6: Read PF Breakdown summary table ───────────────────────────────────
def _find_bad_tickers(tab: str, ticker_col: int = 1, value_col: int = 4) -> list[str]:
    """Return tickers from a sheet tab whose value column is unparseable (e.g. #N/A)."""
    service = _sheets_service(readonly=True)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{tab}'")
        .execute()
    )
    bad = []
    for row in result.get("values", [])[1:]:  # skip header
        ticker = row[ticker_col].strip() if len(row) > ticker_col else ""
        value = row[value_col].strip() if len(row) > value_col else ""
        if ticker and not re.sub(r"[^\d.]", "", value):
            bad.append(f"{ticker}({value!r})")
    return bad


def print_pf_summary() -> None:
    """Find 'PF Breakdown' header dynamically and print a parseable summary line."""
    rows = _read_sheet_rows()

    # Find the header cell by label
    header_row_idx = component_col = None
    for row_idx, row in enumerate(rows):
        for col_idx, cell in enumerate(row):
            if isinstance(cell, str) and cell.strip() == PF_BREAKDOWN_LABEL:
                header_row_idx, component_col = row_idx, col_idx
                break
        if header_row_idx is not None:
            break

    if header_row_idx is None:
        print(f"  WARNING: '{PF_BREAKDOWN_LABEL}' header not found in sheet", file=sys.stderr)
        return

    # Read rows below header: label | amount | percentage
    components = []
    total = None
    for row in rows[header_row_idx + 1:]:
        if len(row) <= component_col:
            continue
        label = str(row[component_col]).strip() if row[component_col] else ""
        amount_raw = row[component_col + 1] if len(row) > component_col + 1 else None
        pct_raw    = row[component_col + 2] if len(row) > component_col + 2 else None

        if amount_raw is None:
            break
        amount_str = re.sub(r"[^\d.]", "", str(amount_raw))
        if not amount_str:
            bad = _find_bad_tickers("US Portfolio") if label == "US PF" else []
            detail = f" — bad tickers: {', '.join(bad)}" if bad else ""
            print(f"  WARNING: PF Breakdown '{label}' amount is unavailable ({amount_raw!r}){detail} — skipping row", file=sys.stderr)
            continue
        amount = float(amount_str)

        if not label:
            total = amount  # blank label = total row
            break
        if pct_raw is not None:
            pct_str = str(pct_raw).strip()
            try:
                if pct_str.endswith("%"):
                    pct = float(pct_str[:-1])
                else:
                    pct = float(pct_str) * 100
                components.append(f"{label} ${amount:,.2f} {pct:.2f}%")
            except ValueError:
                components.append(f"{label} ${amount:,.2f}")

    parts = components + ([f"Total ${total:,.2f}"] if total else [])
    print(f"PF Summary: {' | '.join(parts)}")


# ── Step 5b: Print uninvested cash breakdown ──────────────────────────────────
_UNINVESTED_TICKERS = {"CUR:USD", "FCASH", "FDRXX", "SPAXX"}


def print_uninvested_cash(token: str) -> None:
    """Print per-account uninvested cash as structured log lines (>= $1 only)."""
    accounts = get_monarch_accounts(token)
    brokerage_accounts = [
        a for a in accounts
        if a.get("type", {}).get("name") == "brokerage" and not a.get("deactivatedAt")
    ]

    for acct in brokerage_accounts:
        account_id = acct["id"]
        name = acct.get("displayName", account_id)
        payload = json.dumps({
            "query": _SGOV_HOLDINGS_QUERY,
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
            for holding in node.get("holdings", []):
                if holding.get("ticker") in _UNINVESTED_TICKERS:
                    value = node.get("totalValue", 0.0) or 0.0
                    if value >= 1.0:
                        print(f"[Cash] {name}: ${value:.2f}")
                    break


# ── Step 5c: Print Emergency Fund breakdown ───────────────────────────────────
def print_ef_breakdown(balances: dict[int, float]) -> None:
    """Emit [EF] log lines for each matched account in SHEET_ACCOUNTS."""
    for i, entry in enumerate(SHEET_ACCOUNTS):
        balance = balances.get(i)
        if balance is None:
            continue
        category = entry["sheet_category"]
        institution = entry["sheet_institution"]
        print(f"[EF] {category}|{institution}: ${balance:.2f}")


# ── Step 5d: Print home value ─────────────────────────────────────────────────
def print_home_value(token: str) -> None:
    """Emit [Home] log lines for Zillow home value and mortgage."""
    accounts = get_monarch_accounts(token)
    home_value = None
    mortgage = None
    for acct in accounts:
        if acct.get("deactivatedAt"):
            continue
        acct_type = (acct.get("type") or {}).get("name", "")
        balance = acct.get("displayBalance") or 0.0
        if acct_type == "real_estate" and balance > 0:
            home_value = balance
        elif acct_type == "loan":
            mortgage = abs(balance)
    if home_value is not None:
        print(f"[Home] Value: ${home_value:,.2f}")
    if mortgage is not None:
        print(f"[Home] Mortgage: ${mortgage:,.2f}")
    if home_value is not None and mortgage is not None:
        print(f"[Home] Equity: ${home_value - mortgage:,.2f}")


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
    print("\nFetching account balances from Monarch...")
    balances = get_account_balances(token)
    print(f"  Found {len(balances)} accounts")

    print("Fetching SGOV holdings from Monarch...")
    sgov_total = print_sgov_breakdown(token)
    print(f"  SGOV total: {sgov_total:,.4f} shares")

    print("Fetching uninvested cash from Monarch...")
    print_uninvested_cash(token)

    print("Writing to Google Sheets...")
    update_google_sheet(balances, sgov_total)

    print("Emergency fund balances:")
    print_ef_breakdown(balances)

    print("\nFetching home value from Monarch...")
    print_home_value(token)

    print("\nFetching Monarch net worth...")
    print_net_worth(token)

    print("\nReading PF summary...")
    print_pf_summary()

    print("\nDone.")
