#!/usr/bin/env python3
"""Sync US Portfolio holdings (quantities) from Monarch Money → Google Sheets.

Source of truth: Monarch Money brokerage accounts.

What this does each run:
  - Updates Column D (Quantity) for all tickers already in the sheet
  - Deletes rows for tickers no longer held in Monarch (closed positions)
  - Inserts new rows for tickers in Monarch not yet in the sheet
    (Theme and Conviction Rating are left blank — fill in manually)

Required env vars:
  MONARCH_TOKEN                Monarch Money API token
  GSHEET_SHEET_ID              Google Sheet ID
  GSHEET_SERVICE_ACCOUNT_JSON  Service account JSON (string)

Optional env vars:
  US_PORTFOLIO_TAB             Sheet tab name (default: US Portfolio)
"""

import json
import os
import re
import sys
from collections import defaultdict

from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID = os.environ["GSHEET_SHEET_ID"]
US_PORTFOLIO_TAB = os.getenv("US_PORTFOLIO_TAB", "US Portfolio")

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

# Cash/money-market instruments and tickers managed elsewhere — skip these.
_SKIP_TICKERS = {"CUR:USD", "FCASH", "FDRXX", "SPAXX", "SGOV"}


# ── Google Sheets helpers ─────────────────────────────────────────────────────

def _sheets_service(readonly: bool = True):
    key_info = json.loads(os.environ["GSHEET_SERVICE_ACCOUNT_JSON"])
    scope = (
        "https://www.googleapis.com/auth/spreadsheets.readonly"
        if readonly
        else "https://www.googleapis.com/auth/spreadsheets"
    )
    creds = service_account.Credentials.from_service_account_info(key_info, scopes=[scope])
    return build("sheets", "v4", credentials=creds)


def get_sheet_grid_id() -> int:
    """Return the numeric sheetId (not spreadsheetId) for the US Portfolio tab."""
    service = _sheets_service(readonly=True)
    meta = service.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets.properties",
    ).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == US_PORTFOLIO_TAB:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Tab '{US_PORTFOLIO_TAB}' not found in spreadsheet")


def get_sheet_tickers() -> list[tuple[int, str]]:
    """Return [(row_number, ticker)] for all equity rows (1-indexed, header excluded)."""
    service = _sheets_service(readonly=True)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{US_PORTFOLIO_TAB}'!B:B")
        .execute()
    )
    rows = result.get("values", [])
    tickers = []
    for i, row in enumerate(rows):
        row_num = i + 1
        if row_num == 1:
            continue  # header
        ticker = row[0].strip() if row else ""
        if _TICKER_RE.match(ticker):
            tickers.append((row_num, ticker))
    return tickers


# ── Monarch Money helpers ─────────────────────────────────────────────────────

def _monarch_request(token: str, payload: bytes) -> dict:
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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and attempt < 2:
                import time
                time.sleep(10 * (attempt + 1))  # 10s, then 20s
                continue
            raise


_ACCOUNTS_QUERY = "{ accounts { id displayName deactivatedAt type { name } } }"

_HOLDINGS_QUERY = """
query GetHoldings($accountId: ID!) {
    portfolio(input: { accountIds: [$accountId] }) {
        aggregateHoldings {
            edges {
                node {
                    quantity
                    holdings { ticker }
                }
            }
        }
    }
}
"""


def get_all_holdings(token: str) -> dict[str, float]:
    """Return {ticker: total_quantity} across all active brokerage accounts."""
    result = _monarch_request(token, json.dumps({"query": _ACCOUNTS_QUERY}).encode())
    accounts = result.get("data", {}).get("accounts", [])
    brokerage_ids = [
        a["id"] for a in accounts
        if a.get("type", {}).get("name") == "brokerage" and not a.get("deactivatedAt")
    ]
    print(f"  Found {len(brokerage_ids)} active brokerage accounts")

    totals: dict[str, float] = defaultdict(float)
    for account_id in brokerage_ids:
        payload = json.dumps({
            "query": _HOLDINGS_QUERY,
            "variables": {"accountId": account_id},
        }).encode()
        data = _monarch_request(token, payload)
        edges = (
            data.get("data", {})
            .get("portfolio", {})
            .get("aggregateHoldings", {})
            .get("edges", [])
        )
        for edge in edges:
            node = edge.get("node", {})
            qty = node.get("quantity") or 0.0
            for holding in node.get("holdings", []):
                ticker = holding.get("ticker")
                if ticker:
                    totals[ticker] += qty

    return {
        t: q for t, q in totals.items()
        if t not in _SKIP_TICKERS and _TICKER_RE.match(t)
    }


# ── Sync operations ───────────────────────────────────────────────────────────

def delete_closed_rows(to_remove: set[str], sheet_tickers: list[tuple[int, str]]) -> None:
    """Delete rows for tickers no longer held. Processes in reverse row order."""
    grid_id = get_sheet_grid_id()
    rows_to_delete = sorted(
        [row for row, ticker in sheet_tickers if ticker in to_remove],
        reverse=True,  # reverse so earlier deletions don't shift later indices
    )
    requests = [
        {
            "deleteRange": {
                "range": {
                    "sheetId":       grid_id,
                    "startRowIndex": row - 1,  # 0-indexed
                    "endRowIndex":   row,
                },
                "shiftDimension": "ROWS",
            }
        }
        for row in rows_to_delete
    ]
    _sheets_service(readonly=False).spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": requests},
    ).execute()


def insert_new_rows(
    to_add: set[str],
    holdings: dict[str, float],
    sheet_tickers: list[tuple[int, str]],
) -> None:
    """Insert a row for each new ticker just before the totals row, then write data."""
    if not sheet_tickers:
        insert_before = 2
    else:
        insert_before = max(row for row, _ in sheet_tickers) + 1  # row after last data row

    grid_id = get_sheet_grid_id()
    service = _sheets_service(readonly=False)

    # Insert all blank rows at once (each insert shifts subsequent rows down,
    # so we insert at the same position repeatedly — they stack in order).
    sorted_tickers = sorted(to_add)
    insert_requests = [
        {
            "insertDimension": {
                "range": {
                    "sheetId": grid_id,
                    "dimension": "ROWS",
                    "startIndex": insert_before - 1,  # 0-indexed, insert before totals
                    "endIndex": insert_before,
                },
                "inheritFromBefore": True,
            }
        }
        for _ in sorted_tickers
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": insert_requests},
    ).execute()

    # Write ticker + quantity into the newly inserted rows.
    # First new ticker lands at insert_before, second at insert_before+1, etc.
    value_data = []
    for i, ticker in enumerate(sorted_tickers):
        row = insert_before + i
        qty = round(holdings[ticker], 6)
        value_data.append({
            "range": f"'{US_PORTFOLIO_TAB}'!A{row}:F{row}",
            "values": [[
                "",       # Theme — fill in manually
                ticker,
                f"=E{row}/US_Portfolio[[#TOTALS],[Holdings]]",
                qty,
                # Hardcode ticker in formula — avoids needing a Finance chip in B.
                # (The Sheets API can only write plain text, not chip objects.)
                f'=IFERROR(D{row}*GOOGLEFINANCE("{ticker}"),0)',
                "",       # Conviction — fill in manually
            ]],
        })

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": value_data},
    ).execute()


def _shorten_account_name(display_name: str) -> str:
    """Strip trailing account mask like ' (...8902)' for a compact label."""
    return re.sub(r'\s*\(\.\.\.[^)]*\)\s*$', '', display_name).strip() or display_name


def get_holdings_by_account(token: str) -> dict[str, dict[str, float]]:
    """Return {ticker: {account_short_name: qty}} across all active brokerage accounts."""
    result = _monarch_request(token, json.dumps({"query": _ACCOUNTS_QUERY}).encode())
    accounts = result.get("data", {}).get("accounts", [])
    brokerage_accounts = [
        a for a in accounts
        if a.get("type", {}).get("name") == "brokerage" and not a.get("deactivatedAt")
    ]

    by_account: dict[str, dict[str, float]] = {}
    for account in brokerage_accounts:
        account_id = account["id"]
        short_name = _shorten_account_name(account.get("displayName", account_id))
        payload = json.dumps({
            "query": _HOLDINGS_QUERY,
            "variables": {"accountId": account_id},
        }).encode()
        data = _monarch_request(token, payload)
        edges = (
            data.get("data", {})
            .get("portfolio", {})
            .get("aggregateHoldings", {})
            .get("edges", [])
        )
        for edge in edges:
            node = edge.get("node", {})
            qty = node.get("quantity") or 0.0
            for holding in node.get("holdings", []):
                ticker = holding.get("ticker")
                if not ticker or ticker in _SKIP_TICKERS or not _TICKER_RE.match(ticker):
                    continue
                if ticker not in by_account:
                    by_account[ticker] = {}
                by_account[ticker][short_name] = by_account[ticker].get(short_name, 0.0) + qty

    return by_account


def _format_breakdown(account_qtys: dict[str, float]) -> str:
    """Format per-account quantities as 'AcctA: 5 | AcctB: 10', sorted by account name."""
    parts = [f"{acct}: {qty:g}" for acct, qty in sorted(account_qtys.items())]
    return " | ".join(parts)


def get_sheet_quantities() -> dict[str, float]:
    """Return {ticker: quantity} for all equity rows currently in the sheet."""
    service = _sheets_service(readonly=True)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{US_PORTFOLIO_TAB}'!B:D")
        .execute()
    )
    rows = result.get("values", [])
    quantities = {}
    for i, row in enumerate(rows):
        if i == 0:
            continue  # header
        ticker = row[0].strip() if row else ""
        if not _TICKER_RE.match(ticker):
            continue
        try:
            quantities[ticker] = float(row[2]) if len(row) > 2 else 0.0
        except (ValueError, TypeError):
            quantities[ticker] = 0.0
    return quantities


def update_quantities(
    to_update: set[str],
    holdings: dict[str, float],
    sheet_tickers: list[tuple[int, str]],
) -> None:
    """Write updated quantities to Column D for all tickers in both sources."""
    ticker_to_row = {ticker: row for row, ticker in sheet_tickers}
    value_data = [
        {
            "range": f"'{US_PORTFOLIO_TAB}'!D{ticker_to_row[ticker]}",
            "values": [[round(holdings[ticker], 6)]],
        }
        for ticker in sorted(to_update)
    ]
    _sheets_service(readonly=False).spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": value_data},
    ).execute()


def write_breakdowns(
    breakdown: dict[str, dict[str, float]],
    sheet_tickers: list[tuple[int, str]],
) -> None:
    """Write per-account breakdown text to column G for all tickers in the sheet."""
    ticker_to_row = {ticker: row for row, ticker in sheet_tickers}
    value_data = []
    for ticker, account_qtys in sorted(breakdown.items()):
        if ticker not in ticker_to_row:
            continue
        row = ticker_to_row[ticker]
        value_data.append({
            "range": f"'{US_PORTFOLIO_TAB}'!G{row}",
            "values": [[_format_breakdown(account_qtys)]],
        })
    if not value_data:
        return
    header = [{"range": f"'{US_PORTFOLIO_TAB}'!G1", "values": [["By Account"]]}]
    _sheets_service(readonly=False).spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": header + value_data},
    ).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def sync(token: str) -> None:
    print("Fetching brokerage holdings from Monarch Money...")
    holdings = get_all_holdings(token)
    print(f"  {len(holdings)} equity positions: {sorted(holdings.keys())}")

    print(f"\nReading tickers from '{US_PORTFOLIO_TAB}' tab...")
    sheet_tickers = get_sheet_tickers()
    print(f"  {len(sheet_tickers)} tickers in sheet")

    monarch_set = set(holdings.keys())
    sheet_set = {ticker for _, ticker in sheet_tickers}

    to_update = monarch_set & sheet_set
    to_remove = sheet_set - monarch_set
    to_add = monarch_set - sheet_set

    # ── Step 1: Remove closed positions ──────────────────────────────────────
    if to_remove:
        print(f"\nRemoving {len(to_remove)} closed positions: {sorted(to_remove)}")
        delete_closed_rows(to_remove, sheet_tickers)
        sheet_tickers = get_sheet_tickers()  # re-read after deletions
        for ticker in sorted(to_remove):
            print(f"[US] Closed: {ticker}")
    else:
        print("\nNo closed positions to remove.")

    # ── Step 2: Add new positions ─────────────────────────────────────────────
    if to_add:
        print(f"\nAdding {len(to_add)} new positions: {sorted(to_add)}")
        insert_new_rows(to_add, holdings, sheet_tickers)
        for ticker in sorted(to_add):
            qty = holdings[ticker]
            print(f"  {ticker:6s}: {qty:,.4f} shares (Theme/Conviction: fill manually)")
            print(f"[US] Added: {ticker} +{qty:,.6f}")
        sheet_tickers = get_sheet_tickers()  # re-read after insertions
    else:
        print("No new positions to add.")

    # ── Step 3: Update existing quantities ───────────────────────────────────
    print(f"\nUpdating {len(to_update)} existing positions...")
    if to_update:
        old_quantities = get_sheet_quantities()
        update_quantities(to_update, holdings, sheet_tickers)
        ticker_to_row = {ticker: row for row, ticker in sheet_tickers}
        for ticker in sorted(to_update):
            new_qty = round(holdings[ticker], 6)
            old_qty = round(old_quantities.get(ticker, 0.0), 6)
            diff = round(new_qty - old_qty, 6)
            print(f"  {ticker:6s} → D{ticker_to_row[ticker]}: {holdings[ticker]:,.4f}")
            if diff != 0:
                sign = "+" if diff >= 0 else ""
                print(f"[US] Diff: {ticker} {sign}{diff}")

    # ── Step 4: Write per-account breakdown to column G ──────────────────────
    print("\nFetching per-account breakdown...")
    breakdown = get_holdings_by_account(token)
    write_breakdowns(breakdown, sheet_tickers)
    print(f"  Wrote breakdown for {len(breakdown)} tickers.")

    print(f"\nDone. Updated {len(to_update)}, removed {len(to_remove)}, added {len(to_add)}.")


if __name__ == "__main__":
    sync(os.environ["MONARCH_TOKEN"])
