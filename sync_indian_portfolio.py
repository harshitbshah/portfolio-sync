#!/usr/bin/env python3
"""Sync Indian Portfolio holdings (quantities) from Kite (Zerodha) → Google Sheets.

What this does each run:
  - Updates Column C (Quantity) for all tickers already in the sheet
  - Deletes rows for tickers no longer held in Zerodha (closed positions)
  - Inserts new rows for tickers in Zerodha not yet in the sheet
    (Theme is left blank — fill in manually)
  - Writes Column E (Gain %) = live Holdings / (Kite average price x quantity) - 1

Required env vars:
  KITE_ACCESS_TOKEN            Zerodha enctoken (from kite_auth.py)
  GSHEET_SHEET_ID              Google Sheet ID
  GSHEET_SERVICE_ACCOUNT_JSON  Service account JSON (string)

Optional env vars:
  INDIAN_PORTFOLIO_TAB         Sheet tab name (default: Indian Portfolio)
"""

import json
import os
import re

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

from gain import build_gain_updates, percent_format_request, total_row_after

SHEET_ID            = os.environ["GSHEET_SHEET_ID"]
INDIAN_PORTFOLIO_TAB = os.getenv("INDIAN_PORTFOLIO_TAB", "Indian Portfolio")
KITE_ACCESS_TOKEN   = os.environ["KITE_ACCESS_TOKEN"]  # enctoken from kite_auth.py

# NSE equity symbols start with a letter; this excludes totals/header rows
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9&]{0,19}$")


# ── Kite helpers ──────────────────────────────────────────────────────────────

def get_kite_cash() -> float:
    """Return available cash balance from Zerodha equity margins."""
    r = requests.get(
        "https://kite.zerodha.com/oms/user/margins",
        headers={"Authorization": f"enctoken {KITE_ACCESS_TOKEN}"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Kite margins fetch failed: {data.get('message')}")
    return float(data.get("data", {}).get("equity", {}).get("available", {}).get("live_balance", 0.0))


def get_kite_holdings() -> dict[str, int]:
    """Return {tradingsymbol: quantity} for all settled + T1 DEMAT holdings."""
    r = requests.get(
        "https://kite.zerodha.com/oms/portfolio/holdings",
        headers={"Authorization": f"enctoken {KITE_ACCESS_TOKEN}"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Kite holdings fetch failed: {data.get('message')}")
    return {
        h["tradingsymbol"]: int(h["quantity"]) + int(h.get("t1_quantity", 0))
        for h in data.get("data", [])
        if int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0)) > 0
    }


def get_kite_positions() -> dict[str, int]:
    """Return {tradingsymbol: quantity} for today's CNC buys not yet in holdings.

    Uses the 'net' bucket to avoid duplicates (Kite returns both 'day' and 'net').
    Only includes positions with overnight_quantity == 0 (bought today for the first time).
    """
    r = requests.get(
        "https://kite.zerodha.com/oms/portfolio/positions",
        headers={"Authorization": f"enctoken {KITE_ACCESS_TOKEN}"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Kite positions fetch failed: {data.get('message')}")
    return {
        p["tradingsymbol"]: int(p["quantity"])
        for p in data.get("data", {}).get("net", [])
        if p.get("product") == "CNC"
        and int(p.get("overnight_quantity", 0)) == 0
        and int(p.get("quantity", 0)) > 0
    }


def get_kite_costs() -> dict[str, float]:
    """Return {tradingsymbol: total cost} = Kite average price x quantity.

    Covers settled + T1 holdings and today's first-time CNC buys (same filters as
    get_kite_holdings / get_kite_positions, so costs line up with the quantities).
    """
    headers = {"Authorization": f"enctoken {KITE_ACCESS_TOKEN}"}
    costs: dict[str, float] = {}
    for path, rows_of in (
        ("holdings", lambda d: d.get("data", [])),
        ("positions", lambda d: d.get("data", {}).get("net", [])),
    ):
        r = requests.get(f"https://kite.zerodha.com/oms/portfolio/{path}", headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Kite {path} fetch failed: {data.get('message')}")
        for h in rows_of(data):
            if path == "holdings":
                qty = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
            elif h.get("product") == "CNC" and int(h.get("overnight_quantity", 0)) == 0:
                qty = int(h.get("quantity", 0))
            else:
                continue
            avg = float(h.get("average_price") or 0)
            if qty > 0 and avg > 0:
                costs[h["tradingsymbol"]] = costs.get(h["tradingsymbol"], 0.0) + avg * qty
    return costs


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
    service = _sheets_service(readonly=True)
    meta = service.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets.properties",
    ).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == INDIAN_PORTFOLIO_TAB:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Tab '{INDIAN_PORTFOLIO_TAB}' not found in spreadsheet")


def get_sheet_holdings() -> list[tuple[int, str, int]]:
    """Return [(row_number, ticker, quantity)] for all equity rows (1-indexed, header excluded)."""
    service = _sheets_service(readonly=True)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"'{INDIAN_PORTFOLIO_TAB}'!B:C")
        .execute()
    )
    rows = result.get("values", [])
    holdings = []
    for i, row in enumerate(rows):
        row_num = i + 1
        if row_num == 1:
            continue  # header
        ticker = row[0].strip() if row else ""
        if not _TICKER_RE.match(ticker):
            continue
        qty_raw = row[1].strip() if len(row) > 1 else "0"
        try:
            qty = int(float(qty_raw.replace(",", "")))
        except ValueError:
            qty = 0
        holdings.append((row_num, ticker, qty))
    return holdings


# ── Sync operations ───────────────────────────────────────────────────────────

def delete_closed_rows(to_remove: set[str], sheet_holdings: list[tuple[int, str, int]]) -> None:
    grid_id = get_sheet_grid_id()
    rows_to_delete = sorted(
        [row for row, ticker, _ in sheet_holdings if ticker in to_remove],
        reverse=True,
    )
    reqs = [
        {
            "deleteRange": {
                "range": {
                    "sheetId":       grid_id,
                    "startRowIndex": row - 1,
                    "endRowIndex":   row,
                },
                "shiftDimension": "ROWS",
            }
        }
        for row in rows_to_delete
    ]
    _sheets_service(readonly=False).spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": reqs},
    ).execute()


def insert_new_rows(
    to_add: set[str],
    holdings: dict[str, int],
    sheet_holdings: list[tuple[int, str, int]],
) -> None:
    insert_before = (
        max(row for row, _, _ in sheet_holdings) + 1
        if sheet_holdings else 2
    )
    grid_id = get_sheet_grid_id()
    service = _sheets_service(readonly=False)

    sorted_tickers = sorted(to_add)
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId":    grid_id,
                            "dimension":  "ROWS",
                            "startIndex": insert_before - 1,
                            "endIndex":   insert_before,
                        },
                        "inheritFromBefore": True,
                    }
                }
                for _ in sorted_tickers
            ]
        },
    ).execute()

    value_data = [
        {
            "range": f"'{INDIAN_PORTFOLIO_TAB}'!A{insert_before + i}:C{insert_before + i}",
            "values": [["", ticker, holdings[ticker]]],
        }
        for i, ticker in enumerate(sorted_tickers)
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": value_data},
    ).execute()


def update_quantities(
    to_update: set[str],
    holdings: dict[str, int],
    sheet_holdings: list[tuple[int, str, int]],
) -> None:
    ticker_to_row = {ticker: row for row, ticker, _ in sheet_holdings}
    value_data = [
        {
            "range":  f"'{INDIAN_PORTFOLIO_TAB}'!C{ticker_to_row[ticker]}",
            "values": [[holdings[ticker]]],
        }
        for ticker in sorted(to_update)
    ]
    _sheets_service(readonly=False).spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": value_data},
    ).execute()


def write_gain_column(costs: dict[str, float]) -> list[str]:
    """Write Gain % formulas (column E) for every position row + the totals row."""
    service = _sheets_service(readonly=False)
    col_b = (
        service.spreadsheets().values()
        .get(spreadsheetId=SHEET_ID, range=f"'{INDIAN_PORTFOLIO_TAB}'!B:B")
        .execute().get("values", [])
    )
    rows = [
        (i + 1, row[0].strip()) for i, row in enumerate(col_b)
        if i > 0 and row and _TICKER_RE.match(row[0].strip())
    ]
    total_row = total_row_after(rows, len(col_b))
    data, missing = build_gain_updates(INDIAN_PORTFOLIO_TAB, "D", "E", rows, costs, total_row)
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    end_row = total_row or (max(r for r, _ in rows) if rows else 1)
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [percent_format_request(get_sheet_grid_id(), 4, end_row)]},
    ).execute()
    return missing


# ── Main ──────────────────────────────────────────────────────────────────────

def sync() -> None:
    print("Fetching holdings from Kite (Zerodha)...")
    holdings = get_kite_holdings()
    print(f"  {len(holdings)} settled/T1 holdings: {sorted(holdings.keys())}")

    print("Fetching today's CNC positions from Kite...")
    positions = get_kite_positions()
    if positions:
        print(f"  {len(positions)} intraday CNC buys: {sorted(positions.keys())}")
        for ticker, qty in positions.items():
            if ticker in holdings:
                holdings[ticker] += qty
            else:
                holdings[ticker] = qty
    else:
        print("  No intraday CNC buys today.")
    print(f"  {len(holdings)} total positions: {sorted(holdings.keys())}")

    print(f"\nReading tickers from '{INDIAN_PORTFOLIO_TAB}' tab...")
    sheet_holdings = get_sheet_holdings()
    print(f"  {len(sheet_holdings)} tickers in sheet")

    kite_set  = set(holdings.keys())
    sheet_set = {ticker for _, ticker, _ in sheet_holdings}
    old_qty   = {ticker: qty for _, ticker, qty in sheet_holdings}

    to_update = kite_set & sheet_set
    to_remove = sheet_set - kite_set
    to_add    = kite_set - sheet_set

    # Step 1: remove closed positions
    if to_remove:
        print(f"\nRemoving {len(to_remove)} closed positions: {sorted(to_remove)}")
        delete_closed_rows(to_remove, sheet_holdings)
        sheet_holdings = get_sheet_holdings()
    else:
        print("\nNo closed positions to remove.")

    # Step 2: add new positions
    if to_add:
        print(f"\nAdding {len(to_add)} new positions: {sorted(to_add)}")
        insert_new_rows(to_add, holdings, sheet_holdings)
        for ticker in sorted(to_add):
            print(f"  {ticker}: {holdings[ticker]} shares (Theme: fill manually)")
        sheet_holdings = get_sheet_holdings()
    else:
        print("No new positions to add.")

    # Step 3: update existing quantities
    print(f"\nUpdating {len(to_update)} existing positions...")
    update_quantities(to_update, holdings, sheet_holdings)

    diffs = []
    for ticker in sorted(to_update):
        diff = holdings[ticker] - old_qty.get(ticker, 0)
        sign = "+" if diff >= 0 else ""
        if diff != 0:
            print(f"  {ticker}: {sign}{diff} shares")
            diffs.append((ticker, diff))
        else:
            print(f"  {ticker}: no change")

    unchanged = len(to_update) - len(diffs)
    total_positions = len(holdings)

    # Machine-readable lines for format_email.py
    print(f"\n[Indian] Positions: {total_positions}")
    for ticker, diff in diffs:
        sign = "+" if diff >= 0 else ""
        print(f"[Indian] Diff: {ticker} {sign}{diff}")
    for ticker in sorted(to_remove):
        print(f"[Indian] Closed: {ticker}")
    for ticker in sorted(to_add):
        print(f"[Indian] Added: {ticker} +{holdings[ticker]}")
    print(f"[Indian] Unchanged: {unchanged}")

    print("\nWriting Gain % (live value vs Kite average cost)...")
    try:
        missing = write_gain_column(get_kite_costs())
        for ticker in missing:
            print(f"WARNING: no average price from Kite for {ticker} — Gain % left blank")
        print(f"[Indian] Gain basis missing: {len(missing)}")
    except Exception as e:  # never let the extra column break the quantity sync
        print(f"WARNING: Could not write Gain % (skipping): {e}")

    try:
        cash = get_kite_cash()
        print(f"[Indian] Margin: {cash:.2f}")
    except Exception as e:
        print(f"WARNING: Could not fetch Zerodha margin (skipping): {e}")

    print(f"\nDone. Updated {len(to_update)}, removed {len(to_remove)}, added {len(to_add)}.")


if __name__ == "__main__":
    sync()
