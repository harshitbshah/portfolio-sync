#!/usr/bin/env python3
"""Sync subscriptions from Wallos (self-hosted, on the OCI box) → Google Sheets.

Source of truth: Wallos SQLite DB (only active, i.e. non-inactive, subscriptions).

What this does each run:
  - Inserts a row for each Wallos subscription not yet in the sheet
  - Updates Country/Category/Platform/Status/Price/Expiring On for subscriptions
    already in the sheet. Status is derived from Wallos's auto_renew flag
    (1 → "Subscribed", 0 → "Expiring").
  - Never deletes rows. Sheet entries with no Wallos match are only reported —
    the sheet has long-standing manual entries that predate Wallos and haven't
    been backfilled into it yet, so auto-removal would be destructive.
  - Wallos subscriptions that share a name (e.g. two "Google One" entries) are
    combined into one row named "{name} x{n}" with summed price, matching the
    sheet's existing convention (e.g. "Google One x2") and left without an
    Expiring On date since there's no single date to show.
  - Sorts all rows by Expiring On ascending at the end of every run.

Platform comes from Wallos's free-text `notes` field ("Platform: X" convention,
since Wallos has no dedicated platform field).

Category is translated from Wallos's taxonomy to the sheet's via CATEGORY_MAP,
with NAME_CATEGORY_OVERRIDES for the handful of subscriptions where category-name
translation alone picks the wrong bucket (confirmed against the sheet's prior data).

Required env vars:
  GSHEET_SHEET_ID              Google Sheet ID
  GSHEET_SERVICE_ACCOUNT_JSON  Service account JSON (string)

Optional env vars:
  SUBSCRIPTIONS_TAB   Sheet tab name (default: Subscriptions)
  WALLOS_DB_PATH      Path to Wallos SQLite DB (default: /home/ubuntu/wallos/db/wallos.db)
  WALLOS_SSH_HOST     If set (e.g. "ubuntu@152.70.200.81"), fetch over SSH instead of
                      reading the local file. Unset in production — the GitHub Actions
                      runner for this repo is self-hosted on the same OCI box as Wallos,
                      so it reads the DB file directly. Only needed for local testing
                      from a machine that isn't that box.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SHEET_ID = os.environ["GSHEET_SHEET_ID"]
SUBSCRIPTIONS_TAB = os.getenv("SUBSCRIPTIONS_TAB", "Subscriptions")
WALLOS_DB_PATH = os.getenv("WALLOS_DB_PATH", "/home/ubuntu/wallos/db/wallos.db")
WALLOS_SSH_HOST = os.getenv("WALLOS_SSH_HOST")

# Data columns in the Subscriptions tab (a second, unrelated Category/Total
# pivot table lives in columns K:L on the same rows — never touch outside C:I).
_FIRST_DATA_ROW = 4
_COL_START = 2  # C (0-indexed)
_COL_END = 9    # exclusive → covers C:I

CURRENCY_COUNTRY = {"USD": "United States", "INR": "India"}

# Wallos category → sheet category. Sheet category assignment is content-based,
# not a pure function of Wallos's category name — this is the best general
# mapping; NAME_CATEGORY_OVERRIDES below fixes the known exceptions.
CATEGORY_MAP = {
    "Entertainment": "Streaming",
    "Investing": "Investing",
    "Shopping": "Miscellaneous",
    "Education": "Miscellaneous",
    "Cloud Services": "Miscellaneous",
    "Productivity": "Miscellaneous",
    "Software": "Miscellaneous",
    "Banking": "Miscellaneous",
    "News & Magazines": "News",
}

NAME_CATEGORY_OVERRIDES = {
    "Monarch Money": "Personal",
    "Exponential View": "Tech Newsletter",
    "1 Gbps Internet": "Streaming",
}

_PLATFORM_RE = re.compile(r"Platform:\s*(.+)")

# Wallos cycle id → periods per year (Daily=1, Weekly=2, Monthly=3, Yearly=4).
# The sheet's Price column is an annual total (confirmed by every existing
# Yearly-cycle entry, where price == annual cost) — Wallos stores price per
# billing cycle, so non-Yearly entries must be annualized before writing.
_CYCLE_PERIODS_PER_YEAR = {1: 365, 2: 52, 3: 12, 4: 1}


def _annualize(price: float, cycle: int | None, frequency: int | None) -> float:
    periods = _CYCLE_PERIODS_PER_YEAR.get(cycle, 1)
    return round(price * periods / max(frequency or 1, 1), 2)


def _status_from_auto_renew(auto_renew_values: list[int]) -> str:
    """"Subscribed" if every entry auto-renews, "Expiring" if any doesn't."""
    return "Subscribed" if all(auto_renew_values) else "Expiring"


# ── Wallos helpers ──────────────────────────────────────────────────────────

def _wallos_query(sql: str) -> list[dict]:
    """Run a read-only query against the Wallos SQLite DB and return JSON rows."""
    remote_cmd = f"sudo sqlite3 -json {shlex.quote(WALLOS_DB_PATH)} {shlex.quote(sql)}"
    cmd = ["ssh", WALLOS_SSH_HOST, remote_cmd] if WALLOS_SSH_HOST else shlex.split(remote_cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else []


def _parse_platform(notes: str | None) -> str:
    if not notes:
        return "Independent"
    match = _PLATFORM_RE.search(notes)
    return match.group(1).strip() if match else "Independent"


def get_wallos_subscriptions() -> list[dict]:
    """Return raw active-subscription rows from Wallos, joined with category/currency names."""
    sql = (
        "SELECT s.name, s.price, cur.code AS currency, s.next_payment, "
        "s.cycle, s.frequency, s.auto_renew, s.notes, cat.name AS category "
        "FROM subscriptions s "
        "LEFT JOIN currencies cur ON s.currency_id = cur.id "
        "LEFT JOIN categories cat ON s.category_id = cat.id "
        "WHERE s.inactive = 0;"
    )
    return _wallos_query(sql)


def build_desired_rows(raw_rows: list[dict]) -> dict[str, dict]:
    """Group raw Wallos rows by name and build the desired sheet state.

    Subscriptions sharing a name are combined into one "{name} xN" row with
    summed price and no Expiring On date (matches the sheet's "Google One x2"
    convention — there's no single date to show for a combined row).
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in raw_rows:
        groups[row["name"]].append(row)

    desired = {}
    for name, entries in groups.items():
        first = entries[0]
        currency = first.get("currency") or ""
        country = CURRENCY_COUNTRY.get(currency, currency)
        wallos_category = first.get("category") or ""
        sheet_category = NAME_CATEGORY_OVERRIDES.get(
            name, CATEGORY_MAP.get(wallos_category, "Miscellaneous")
        )
        platform = _parse_platform(first.get("notes"))
        status = _status_from_auto_renew([e.get("auto_renew", 1) for e in entries])

        if len(entries) > 1:
            key = f"{name} x{len(entries)}"
            price = round(
                sum(_annualize(e["price"], e.get("cycle"), e.get("frequency")) for e in entries),
                2,
            )
            expiring = None
        else:
            key = name
            price = _annualize(first["price"], first.get("cycle"), first.get("frequency"))
            expiring = first.get("next_payment")

        desired[key] = {
            "country": country,
            "category": sheet_category,
            "platform": platform,
            "status": status,
            "price": price,
            "expiring": expiring,
        }
    return desired


# ── Google Sheets helpers ────────────────────────────────────────────────────

def _sheets_service(readonly: bool = True):
    key_info = json.loads(os.environ["GSHEET_SERVICE_ACCOUNT_JSON"])
    scope = (
        "https://www.googleapis.com/auth/spreadsheets.readonly"
        if readonly
        else "https://www.googleapis.com/auth/spreadsheets"
    )
    creds = service_account.Credentials.from_service_account_info(key_info, scopes=[scope])
    return build("sheets", "v4", credentials=creds)


def _get_tab_grid_id(service, tab_name: str) -> int:
    meta = service.spreadsheets().get(
        spreadsheetId=SHEET_ID, fields="sheets.properties"
    ).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == tab_name:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab_name}' not found in spreadsheet")


def get_sheet_subscriptions() -> dict[str, dict]:
    """Return {service_name: {row, country, category, platform, status, expiring, price}}."""
    service = _sheets_service(readonly=True)
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SHEET_ID,
            range=f"'{SUBSCRIPTIONS_TAB}'!C{_FIRST_DATA_ROW}:I",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )
    rows = result.get("values", [])
    subs = {}
    for i, row in enumerate(rows):
        row_num = _FIRST_DATA_ROW + i
        service_name = str(row[3]).strip() if len(row) > 3 else ""
        if not service_name:
            continue
        subs[service_name] = {
            "row": row_num,
            "country": row[0] if len(row) > 0 else "",
            "category": row[1] if len(row) > 1 else "",
            "platform": row[2] if len(row) > 2 else "",
            "status": row[4] if len(row) > 4 else "",
            "expiring": row[5] if len(row) > 5 else "",
            "price": row[6] if len(row) > 6 else "",
        }
    return subs


def _format_date(expiring: str | None) -> str | None:
    if not expiring:
        return None
    d = datetime.strptime(expiring, "%Y-%m-%d")
    return f"{d.month}/{d.day}/{d.year}"


def update_matched(service, to_update: set[str], desired: dict, sheet_subs: dict) -> None:
    """Overwrite Country/Category/Platform/Status/Price (and Expiring On, if known)."""
    data = []
    for key in to_update:
        row = sheet_subs[key]["row"]
        d = desired[key]
        data.append({"range": f"'{SUBSCRIPTIONS_TAB}'!C{row}", "values": [[d["country"]]]})
        data.append({"range": f"'{SUBSCRIPTIONS_TAB}'!D{row}", "values": [[d["category"]]]})
        data.append({"range": f"'{SUBSCRIPTIONS_TAB}'!E{row}", "values": [[d["platform"]]]})
        data.append({"range": f"'{SUBSCRIPTIONS_TAB}'!G{row}", "values": [[d["status"]]]})
        data.append({"range": f"'{SUBSCRIPTIONS_TAB}'!I{row}", "values": [[d["price"]]]})
        formatted_date = _format_date(d["expiring"])
        if formatted_date:
            data.append({"range": f"'{SUBSCRIPTIONS_TAB}'!H{row}", "values": [[formatted_date]]})
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()


def insert_new(service, to_add: set[str], desired: dict, sheet_subs: dict) -> None:
    """Insert new rows, scoped to columns C:I only so the K:L pivot table is untouched."""
    if not to_add:
        return
    grid_id = _get_tab_grid_id(service, SUBSCRIPTIONS_TAB)
    last_row = max((s["row"] for s in sheet_subs.values()), default=_FIRST_DATA_ROW - 1)
    insert_at = last_row + 1
    sorted_keys = sorted(to_add)
    n = len(sorted_keys)

    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{
            "insertRange": {
                "range": {
                    "sheetId": grid_id,
                    "startRowIndex": insert_at - 1,
                    "endRowIndex": insert_at - 1 + n,
                    "startColumnIndex": _COL_START,
                    "endColumnIndex": _COL_END,
                },
                "shiftDimension": "ROWS",
            }
        }]},
    ).execute()

    value_data = []
    for i, key in enumerate(sorted_keys):
        row = insert_at + i
        d = desired[key]
        formatted_date = _format_date(d["expiring"]) or ""
        value_data.append({
            "range": f"'{SUBSCRIPTIONS_TAB}'!C{row}:I{row}",
            "values": [[
                d["country"], d["category"], d["platform"], key,
                d["status"],
                formatted_date,
                d["price"],
            ]],
        })
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": value_data},
    ).execute()


def sort_by_expiring(service, last_row: int) -> None:
    """Sort all data rows by Expiring On (column H) ascending.

    Scoped to columns C:I only, like every other row operation here, so the
    K:L pivot table (a separate Table object on the same rows) isn't touched.
    """
    if last_row < _FIRST_DATA_ROW:
        return
    grid_id = _get_tab_grid_id(service, SUBSCRIPTIONS_TAB)
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{
            "sortRange": {
                "range": {
                    "sheetId": grid_id,
                    "startRowIndex": _FIRST_DATA_ROW - 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": _COL_START,
                    "endColumnIndex": _COL_END,
                },
                "sortSpecs": [{"dimensionIndex": 7, "sortOrder": "ASCENDING"}],  # H
            }
        }]},
    ).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def sync() -> None:
    print("Fetching active subscriptions from Wallos...")
    raw = get_wallos_subscriptions()
    desired = build_desired_rows(raw)
    print(f"  {len(desired)} subscriptions (from {len(raw)} raw Wallos rows)")

    print(f"\nReading '{SUBSCRIPTIONS_TAB}' tab...")
    sheet_subs = get_sheet_subscriptions()
    print(f"  {len(sheet_subs)} rows in sheet")

    desired_keys = set(desired)
    sheet_keys = set(sheet_subs)
    to_update = desired_keys & sheet_keys
    to_add = desired_keys - sheet_keys
    not_in_wallos = sheet_keys - desired_keys

    service = _sheets_service(readonly=False)

    if to_update:
        print(f"\nUpdating {len(to_update)} existing rows: {sorted(to_update)}")
        update_matched(service, to_update, desired, sheet_subs)
    else:
        print("\nNo existing rows to update.")

    if to_add:
        print(f"\nAdding {len(to_add)} new rows: {sorted(to_add)}")
        insert_new(service, to_add, desired, sheet_subs)
    else:
        print("No new rows to add.")

    if not_in_wallos:
        print(
            f"\n{len(not_in_wallos)} sheet rows have no Wallos match "
            f"(not removed — review manually): {sorted(not_in_wallos)}"
        )

    print("\nSorting by Expiring On (ascending)...")
    original_last_row = max((s["row"] for s in sheet_subs.values()), default=_FIRST_DATA_ROW - 1)
    last_row = original_last_row + len(to_add)
    sort_by_expiring(service, last_row)

    print(f"\nDone. Updated {len(to_update)}, added {len(to_add)}, "
          f"{len(not_in_wallos)} unmatched sheet rows left untouched.")


if __name__ == "__main__":
    sync()
